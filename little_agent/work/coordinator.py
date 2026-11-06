"""Work lifecycle and human gates. Domain decisions remain in Learning Engine."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from little_agent.agent import Agent, RunInterrupted
from little_agent.messages import Message
from little_agent.tools.base import ToolContext, ToolResult
from little_agent.work.learning_client import LearningClient, MUTATIONS
from little_agent.work.models import LearningError, WorkCheckpoint, identity

# Explicit trust, never inferred from requires_confirmation or an LLM assertion.
READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "get_datetime"})
RUNNABLE = {"READY", "EXPLORE"}
DECISIONS = RUNNABLE | {"NEED_INPUT", "REVIEW", "BLOCK", "READY_TO_RESUME"}
RULES = (
    "判断が必要な場合は work_evaluate を使う。初めての概念、知識の適用可否、"
    "選択基準の不足、過去の失敗に似た条件、未確認の重要仮定、人間固有の条件が対象。"
    "質問が出たら操作を続けない。根拠は承認済み知識と実際のTool結果に限定する。"
    "意味のある作業単位を work_record_progress で action/reason/result/evidence/state として記録する。"
    "executed は成功したTool結果のoperation_idを evidence に含める。計画は planned、推測は hypothesis。"
    "成功済みの操作を別の引数表現で繰り返さない。完了候補を人間に提示し /work finish を案内する。"
    "回答確認・知識の承認や公開は人間専用。仕事の最終文章は仕事の完了を意味しない。"
)


class WorkCoordinator:
    def __init__(
        self, client: LearningClient, checkpoint_path: Path, *,
        conversation_id: str | None = None,
        required_check: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        self.client = client
        self.path = checkpoint_path
        self.conversation_id = conversation_id or str(uuid4())
        # Additional business-specific mandatory gates; mutating tools are
        # always checked regardless of this optional policy.
        self.required_check = required_check or (lambda _name, _args: False)
        self.work: WorkCheckpoint | None = None
        self.allowed_exploration: set[str] = set()
        self.knowledge: list[dict[str, Any]] = []
        self.resume_context: dict[str, Any] = {}
        self.answer_token: str | None = None
        self.review_tokens: dict[str, dict[str, Any]] = {}
        self.agent: Agent | None = None
        self._lock_file = None
        self._acquire()
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if raw.get("version") != 1:
                    raise LearningError("未対応のWorkチェックポイントです。")
                self.work = WorkCheckpoint(**raw["work"])
                self.conversation_id = self.work.conversation_id
                # Never silently continue a restored run, even if it was READY.
                if self.work.state in RUNNABLE:
                    self.work.state = "PAUSED"
                    self._save()
        except BaseException:
            self.close()
            raise

    def _acquire(self) -> None:
        """One coordinator per checkpoint, with a crash-released OS lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_suffix(".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LearningError("このworkspaceは別の学習Chatが使用中です。") from exc
        self._lock_file = stream

    def close(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _save(self) -> None:
        if self.work is None:
            return
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"version": 1, "work": self.work.as_dict()}, stream,
                      ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def _require_work(self) -> WorkCheckpoint:
        if self.work is None:
            raise LearningError("WorkSessionがありません。/work start <目的> で開始してください。")
        return self.work

    def _fail(self, exc: Exception) -> str:
        if self.work:
            if self.work.state != "FINISHED":
                self.work.state = "ERROR"
            self.work.error = str(exc)
            self._save()
        return f"[learning] 停止しました: {exc}\n知識を参照できない場合は推測で継続しません。/mcp reconnect learning_engine 後に /work resume。"

    def _call(self, name: str, **arguments: Any) -> dict[str, Any]:
        work = self.work
        if work and name in MUTATIONS:
            # Each logical call gets a new identity; an ambiguous failure keeps
            # the sequence so retrying that call is idempotent across restart.
            arguments["request_id"] = identity([work.work_id, name, arguments,
                                                work.rpc_sequences.get(name, 0)])
        try:
            data = self.client.call(name, **arguments)
        except LearningError as exc:
            raise RunInterrupted(self._fail(exc)) from exc
        if work and name in MUTATIONS:
            work.rpc_sequences[name] = work.rpc_sequences.get(name, 0) + 1
            self._save()
        return data

    def _load_knowledge(self) -> None:
        work = self._require_work()
        self.knowledge = []
        try:
            self.knowledge = self.client.approved_knowledge(work.work_id, work.goal)
        except LearningError as exc:
            raise RunInterrupted(self._fail(exc)) from exc

    def bind(self, agent: Agent) -> None:
        if agent.memory.enabled:
            raise LearningError("学習対応モードではNullMemoryStoreを使用してください。専門知識はLearning Engineが管理します。")
        self.agent = agent
        for name, callback, properties, required in (
            ("work_evaluate", self.evaluate,
             {"target": {"type": "string", "minLength": 1},
              "facts": {"type": "object"}}, ["target", "facts"]),
            ("work_record_progress", self.record_progress,
             {"action": {"type": "string", "minLength": 1},
              "reason": {"type": "string"}, "result": {"type": "string"},
              "evidence": {"type": "array", "items": {"type": "string"}},
              "state": {"enum": ["executed", "planned", "hypothesis"]}},
             ["action", "reason", "result", "evidence", "state"]),
        ):
            agent.tools.register(_CoordinatorTool(name, callback, properties, required))

    def start(self, goal: str) -> None:
        if not goal.strip():
            raise LearningError("仕事の目的を指定してください。")
        if self.work and (self.work.state != "FINISHED" or self.work.reflection_pending):
            raise LearningError("未完了の仕事があります。/work status または /work resume を使ってください。")
        self.work = WorkCheckpoint(str(uuid4()), self.conversation_id, goal.strip())
        self.answer_token = None
        self.review_tokens.clear()
        self.knowledge = []
        self.resume_context = {}
        self._save()  # intent survives an ambiguous start RPC
        self._start_remote()

    def _start_remote(self) -> None:
        work = self._require_work()
        data = self._call("le_start_work", work_id=work.work_id,
                          conversation_id=work.conversation_id, goal=work.goal)
        if data.get("work_id") != work.work_id:
            raise LearningError("Learning Engine が別の work_id を返しました。")
        self._load_knowledge()
        self.evaluate("work_start", {"goal": work.goal})

    def check(self) -> None:
        work = self._require_work()
        if work.state not in RUNNABLE or work.questions or work.pending_answer:
            raise RunInterrupted(self.status())

    def evaluate(self, target: str, facts: dict[str, Any]) -> dict[str, Any]:
        work = self._require_work()
        if work.questions or work.pending_answer:
            raise RunInterrupted(self.status())
        if not isinstance(target, str) or not target.strip() or not isinstance(facts, dict):
            raise LearningError("判断対象と事実を指定してください。")
        work.decision_target = target
        work.state = "EVALUATING"
        self._save()
        data = self._call("le_evaluate", work_id=work.work_id, target=target, facts=facts)
        return self._apply_decision(data)

    def _apply_decision(self, data: dict[str, Any]) -> dict[str, Any]:
        work = self._require_work()
        state = data.get("state")
        if state not in DECISIONS:
            raise LearningError("Learning Engine が不正な判断状態を返しました。")
        questions = data.get("questions", [])
        evidence = data.get("evidence", [])
        if not isinstance(questions, list) or any(
            not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q["id"]
            or not isinstance(q.get("text"), str) or not q["text"] for q in questions
        ):
            raise LearningError("Learning Engine の質問形式が不正です。")
        if not isinstance(evidence, list) or any(not isinstance(e, str) for e in evidence):
            raise LearningError("Learning Engine の根拠形式が不正です。")
        if state in {"READY", "EXPLORE", "READY_TO_RESUME"} and questions:
            raise LearningError("未回答の質問と実行可能状態が矛盾しています。")
        if state == "NEED_INPUT" and not questions:
            raise LearningError("NEED_INPUT に質問がありません。")
        unique = {q["id"]: {"id": q["id"], "text": q["text"]} for q in questions}
        if any(unique[q["id"]]["text"] != q["text"] for q in questions):
            raise LearningError("同じ質問IDに異なる質問文が含まれています。")
        allowed = data.get("allowed_tools", [])
        if not isinstance(allowed, list) or any(not isinstance(n, str) for n in allowed):
            raise LearningError("調査許可範囲が不正です。")
        work.state = state
        work.questions = list(unique.values())
        work.evidence = evidence
        work.error = ""
        if state not in RUNNABLE and not work.next_action:
            work.next_action = work.decision_target
        self.allowed_exploration = set(allowed) & READ_ONLY_TOOLS
        self._save()
        if state not in RUNNABLE:
            raise RunInterrupted(self.status())
        return {"state": state, "evidence": evidence}

    def before_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        self.check()
        if name in {"work_evaluate", "work_record_progress"}:
            return None
        work = self._require_work()
        op_id = identity([name, arguments])
        previous = work.operations.get(op_id)
        if previous:
            if previous["state"] == "executed":
                return ToolResult(True, previous["result"] + f"\noperation_id={op_id} (保存結果)")
            raise RunInterrupted(f"操作 {op_id} は {previous['state']} です。結果を確認し /work resolve で解決してください。")
        if work.state == "EXPLORE" and name not in self.allowed_exploration:
            raise RunInterrupted("EXPLORE の許可範囲外です。調査対象を再評価してください。")
        work.next_action = name
        self._save()
        if name not in READ_ONLY_TOOLS or self.required_check(name, arguments):
            self.evaluate(f"before_tool:{name}", {"operation_id": op_id, "arguments": arguments})
            if work.state == "EXPLORE" and name not in self.allowed_exploration:
                raise RunInterrupted("この操作は調査許可範囲外です。")
        work.operations[op_id] = {"name": name, "arguments": arguments,
                                  "state": "in_flight", "result": ""}
        self._save()  # reserve before any side effect
        return None

    def after_tool(self, name: str, arguments: dict[str, Any], result: ToolResult) -> ToolResult | None:
        if name in {"work_evaluate", "work_record_progress"}:
            return
        work = self._require_work()
        op_id = identity([name, arguments])
        operation = work.operations[op_id]
        operation.update(state="executed" if result.ok else "uncertain", result=result.content)
        work.next_action = ""
        self._save()
        if not result.ok:
            work.state = "PAUSED"
            self._save()
            raise RunInterrupted(f"操作 {op_id} が失敗しました。副作用の有無を確認し /work resolve で解決してください。")
        return ToolResult(True, result.content + f"\noperation_id={op_id}", result.images)

    def record_progress(self, action: str, reason: str, result: str,
                        evidence: list[str], state: str) -> dict[str, Any]:
        self.check()
        work = self._require_work()
        if state not in {"executed", "planned", "hypothesis"} or not action.strip():
            raise LearningError("作業記録の action/state が不正です。")
        if not isinstance(evidence, list) or any(not isinstance(e, str) for e in evidence):
            raise LearningError("evidence は文字列配列で指定してください。")
        actual = [e for e in evidence if e in work.operations and work.operations[e]["state"] == "executed"]
        if state == "executed" and not actual:
            raise LearningError("executed には成功したTool結果の operation_id が必要です。")
        unit = {"action": action, "reason": reason, "result": result, "evidence": evidence, "state": state}
        unit_id = identity(unit)
        response = self._call("le_record_progress", work_id=work.work_id, unit_id=unit_id,
                              **unit, tool_results={e: work.operations[e]["result"] for e in actual})
        if state == "executed":
            work.last_action = action
            work.next_action = ""
            if unit_id not in work.recorded_units:
                work.recorded_units.append(unit_id)
        else:
            work.next_action = action
        self._save()
        return response

    def receive_answer(self, text: str) -> str:
        work = self._require_work()
        if not work.questions:
            raise LearningError("回答待ちの質問がありません。")
        q = work.questions[0]
        data = self._call("le_record_answer", work_id=work.work_id,
                          question_id=q["id"], answer=text)
        if not isinstance(data.get("answer_id"), str) or not data["answer_id"] or not isinstance(data.get("interpretation"), str):
            raise LearningError("回答解釈の応答が不正です。")
        work.pending_answer = {"answer_id": data["answer_id"], "question_id": q["id"],
                               "interpretation": data["interpretation"]}
        self._save()
        return self.answer_preview()

    def answer_preview(self) -> str:
        answer = self._require_work().pending_answer
        if not answer:
            raise LearningError("確認待ちの回答がありません。")
        self.answer_token = secrets.token_urlsafe(18)
        return (f"回答の解釈: {answer['interpretation']}\n"
                f"確認: /work confirm {self.answer_token}\n修正する場合は回答をもう一度入力してください。")

    def confirm_answer(self, token: str) -> bool:
        work = self._require_work()
        answer = work.pending_answer
        if not answer or not self.answer_token or not secrets.compare_digest(token, self.answer_token):
            raise LearningError("回答確認トークンが無効です。/work answer で表示してください。")
        self._call("le_confirm_answer", work_id=work.work_id, answer_id=answer["answer_id"])
        work.questions = [q for q in work.questions if q["id"] != answer["question_id"]]
        work.pending_answer = None
        self.answer_token = None
        work.state = "NEED_INPUT" if work.questions else "READY_TO_RESUME"
        self._save()
        if work.questions:
            return False
        return self.resume()

    def pause(self) -> None:
        work = self._require_work()
        if work.state == "FINISHED":
            raise LearningError("完了済みです。")
        work.state = "PAUSED"
        self._save()  # local gate is effective even if remote pause fails
        self._call("le_pause_work", work_id=work.work_id, checkpoint=self._resume_checkpoint())

    def _resume_checkpoint(self) -> dict[str, Any]:
        work = self._require_work()
        return {"goal": work.goal, "last_action": work.last_action, "next_action": work.next_action,
                "decision_target": work.decision_target, "questions": work.questions,
                "evidence": work.evidence,
                "operations": {key: {k: op[k] for k in ("name", "state", "result")}
                               for key, op in work.operations.items()}}

    def resume(self) -> bool:
        work = self._require_work()
        if work.state == "FINISHED":
            raise LearningError("完了済みの仕事は再開できません。/work review を使ってください。")
        if work.questions or work.pending_answer:
            raise RunInterrupted(self.status())
        if any(op["state"] != "executed" for op in work.operations.values()):
            raise LearningError("結果不明の操作があります。/work status と /work resolve で確認してください。")
        # Retry an ambiguous start using the same work_id and request_id.
        self._call("le_start_work", work_id=work.work_id,
                   conversation_id=work.conversation_id, goal=work.goal)
        data = self._call("le_get_resume_context", work_id=work.work_id)
        # Recover questions whose original evaluation reply was lost.
        if data.get("state") in {"NEED_INPUT", "REVIEW", "BLOCK"}:
            self._apply_decision(data)
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            raise LearningError("再開コンテキストの質問形式が不正です。")
        if data.get("state") not in {"READY", "READY_TO_RESUME"} or questions:
            work.state = "PAUSED"
            self._save()
            raise LearningError("Learning Engine が再開を許可していません。")
        context = data.get("context", {})
        if not isinstance(context, dict):
            raise LearningError("再開コンテキストが不正です。")
        # Do not replay unapproved knowledge or arbitrary server metadata.
        self.resume_context = {k: context[k] for k in ("last_action", "next_action", "confirmed_answers") if k in context}
        self._load_knowledge()
        work.state = "READY_TO_RESUME"
        self._save()
        self._call("le_resume_work", work_id=work.work_id, checkpoint=self._resume_checkpoint())
        self.evaluate("work_resume", {"checkpoint": self._resume_checkpoint()})
        return True

    def resolve_operation(self, op_id: str, outcome: str, evidence: str) -> None:
        work = self._require_work()
        op = work.operations.get(op_id)
        if not op or op["state"] == "executed" or outcome not in {"done", "retry"} or not evidence.strip():
            raise LearningError("/work resolve <operation_id> done|retry <実行結果を確認した根拠> を指定してください。")
        if outcome == "done":
            op.update(state="executed", result=f"人間による実行結果確認: {evidence}")
        else:
            del work.operations[op_id]
        work.state = "PAUSED"
        work.error = ""
        self._save()

    def finish(self) -> str:
        work = self._require_work()
        if work.state == "FINISHED":
            return self.review()
        if work.completion is not None:
            # A lost finish reply must retry the committed completion intent,
            # never reopen the already completed remote job for tool execution.
            self._call("le_finish_work", work_id=work.work_id, completion=work.completion)
            work.state = "FINISHED"
            self._save()
            return self.review()
        self.check()
        if work.next_action or not work.recorded_units or any(
            op["state"] != "executed" for op in work.operations.values()
        ):
            raise LearningError("完了には実行結果の記録と残作業の解消が必要です。")
        self.evaluate("work_finish", {"goal": work.goal, "last_action": work.last_action,
                                      "executed_units": work.recorded_units, "human_requested": True})
        if work.state != "READY":
            raise LearningError("完了の必須確認が済んでいません。")
        work.completion = {"goal_achieved": True, "last_action": work.last_action,
                           "executed_units": work.recorded_units}
        work.next_action = ""
        work.reflection_pending = True
        self._save()
        self._call("le_finish_work", work_id=work.work_id, completion=work.completion)
        work.state = "FINISHED"
        self._save()
        return self.review()

    def review(self) -> str:
        work = self._require_work()
        if work.state != "FINISHED":
            raise LearningError("振り返りは /work finish の後で実行します。")
        if work.reflection_pending:
            self._call("le_reflect", work_id=work.work_id)
            work.reflection_pending = False
            self._save()
        data = self._call("le_get_review", work_id=work.work_id)
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise LearningError("学習候補の形式が不正です。")
        self.review_tokens.clear()
        lines = ["今回の仕事から次の知識候補が得られました"]
        labels = {"criterion": "判断基準", "procedure": "手順", "unresolved": "未解決事項"}
        for candidate in candidates:
            if not isinstance(candidate, dict) or any(
                not isinstance(candidate.get(k), str) or not candidate[k]
                for k in ("id", "revision", "kind", "text", "status")
            ):
                raise LearningError("学習候補にID・版・本文・状態が必要です。")
            lines.append(f"- {labels.get(candidate['kind'], candidate['kind'])}: {candidate['text']} ({candidate['status']})")
            if candidate["status"] in {"pending", "deferred"}:
                token = secrets.token_urlsafe(18)
                self.review_tokens[token] = {k: candidate[k] for k in ("id", "revision")}
                lines.append(f"  /work review approve|defer|reject|edit {token} [修正文]")
        return "\n".join(lines)

    def review_action(self, action: str, token: str, text: str = "") -> str:
        work = self._require_work()
        if work.state != "FINISHED" or action not in {"approve", "defer", "reject", "edit"}:
            raise LearningError("レビュー操作が不正です。")
        candidate = self.review_tokens.get(token)
        if candidate is None or (action == "edit" and not text.strip()):
            raise LearningError("候補トークンが無効、または修正文がありません。/work review で再表示してください。")
        self._call("le_review_candidate", work_id=work.work_id,
                   candidate_id=candidate["id"], revision=candidate["revision"],
                   action=action, text=text)
        self.review_tokens.clear()
        return self.review()  # edits require a fresh review and separate approval

    def status(self) -> str:
        if self.work is None:
            return "WorkSessionはありません。目的を入力すると開始します。"
        work = self.work
        lines = [f"Work: {work.work_id}", f"目的: {work.goal}", f"状態: {work.state}",
                 f"最後の作業: {work.last_action or '未実行'}", f"次の作業: {work.next_action or '未設定'}",
                 f"判断対象: {work.decision_target or '未設定'}"]
        lines.extend(f"質問 [{q['id']}]: {q['text']}" for q in work.questions)
        lines.extend(f"操作 {key}: {op['name']} ({op['state']})" for key, op in work.operations.items())
        if work.pending_answer:
            lines.append("回答解釈の確認待ちです。/work answer で表示してください。")
        if work.error:
            lines.append(f"エラー: {work.error}")
        return "\n".join(lines)

    def send(self, agent: Agent, text: str, history: list[Message]) -> str:
        try:
            self.bind(agent)
            if self.work and self.work.questions:
                return self.receive_answer(text)
            if self.work is None or self.work.state == "FINISHED":
                self.start(text)
            self.check()
            return self._run(agent, text)
        except (LearningError, RunInterrupted) as exc:
            return self._fail(exc) if isinstance(exc, LearningError) else str(exc)

    def _run(self, agent: Agent, instruction: str) -> str:
        # Work context is independent of the ChatSession transcript: reflection
        # drafts and raw expert answers are never reusable knowledge by history.
        result = agent.run(instruction, context={"work": self._resume_checkpoint(),
                           "work_id": self._require_work().work_id, "rules": RULES,
                           "approved_knowledge": self.knowledge,
                           "resume": self.resume_context}, hooks=self)
        if (result.interrupted or agent.stop.triggered) and self._require_work().state in RUNNABLE:
            self._require_work().state = "PAUSED"
            self._save()
        return result.text

    def command(self, agent: Agent, arguments: str) -> str:
        """Trusted CLI entry point. Never registered as an LLM tool."""
        action, _, rest = arguments.strip().partition(" ")
        try:
            self.bind(agent)
            if action == "start":
                self.start(rest)
                return self._run(agent, rest)
            if action in {"", "status"}:
                return self.status()
            if action == "pause":
                self.pause()
                return self.status()
            if action == "resume":
                if self.resume():
                    return self._run(agent, "保存されたチェックポイントから仕事を再開してください。")
            if action == "finish":
                return self.finish()
            if action == "answer":
                return self.answer_preview()
            if action == "confirm":
                if self.confirm_answer(rest.strip()):
                    return self._run(agent, "確認済みの回答とチェックポイントから仕事を再開してください。")
                return self.status()
            if action == "review":
                if not rest:
                    return self.review()
                words = rest.split(maxsplit=2)
                if len(words) >= 2:
                    return self.review_action(words[0], words[1], words[2] if len(words) > 2 else "")
            if action == "list":
                return json.dumps(self._call("le_list_work"), ensure_ascii=False, indent=2)
            if action == "open":
                if self.work and (self.work.state != "FINISHED" or self.work.reflection_pending):
                    raise LearningError("現在の仕事と振り返りを先に完了してください。")
                data = self._call("le_get_resume_context", work_id=rest.strip())
                if not isinstance(data.get("goal"), str) or data.get("state") != "FINISHED":
                    raise LearningError("過去のレビューを開くには完了済みWorkSessionが必要です。")
                self.work = WorkCheckpoint(rest.strip(), self.conversation_id, data["goal"], state="FINISHED")
                self._save()
                return self.review()
            if action == "resolve":
                words = rest.split(maxsplit=2)
                if len(words) == 3:
                    self.resolve_operation(*words)
                    return self.status()
            if action == "chat":
                if self.work and self.work.state != "FINISHED":
                    raise LearningError("未完了の仕事があります。質問・作業を先に解決してください。")
                return agent.run(rest).text
            return ("/work start <目的> | status | pause | resume | finish | review | list\n"
                    "/work answer | confirm <token> | review approve|edit|defer|reject <token> [修正文]\n"
                    "/work resolve <operation_id> done|retry <根拠> | open <完了済みwork_id> | chat <通常会話>")
        except (LearningError, RunInterrupted) as exc:
            # User input errors must not overwrite a legitimate waiting state.
            return f"[learning] {exc}"


class _CoordinatorTool:
    requires_confirmation = False

    def __init__(self, name: str, callback, properties: dict[str, Any], required: list[str]) -> None:
        self.name = name
        self.description = RULES
        self.callback = callback
        self.parameters = {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}

    def run(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        from little_agent.schema import validate
        errors = validate(kwargs, self.parameters)
        if errors:
            raise RunInterrupted("作業ツールの引数が不正です: " + "; ".join(errors))
        try:
            return ToolResult(True, json.dumps(self.callback(**kwargs), ensure_ascii=False))
        except LearningError as exc:
            raise RunInterrupted(str(exc)) from exc
