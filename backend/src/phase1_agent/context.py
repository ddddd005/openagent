"""Canonical prompt history management for phase-one runs."""

from __future__ import annotations

from copy import deepcopy
import json
from threading import RLock
from typing import Any, Sequence

from .contracts import Message, RunResult


class PromptContext:
    """Build isolated model prompts and commit only dispatched run history."""

    def __init__(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._history: list[Message] = []
        self._pending: tuple[str, str, Message, tuple[Message, ...]] | None = None
        self._completed: dict[str, RunResult] = {}
        self._lock = RLock()

    @property
    def history(self) -> tuple[Message, ...]:
        with self._lock:
            return tuple(deepcopy(self._history))

    def import_history(self, records: Sequence[Message]) -> None:
        """Accept externally persisted canonical history only after integrity checks."""
        with self._lock:
            if self._history or self._pending is not None or self._completed:
                raise RuntimeError("history can only be imported into a fresh context")
            snapshot = deepcopy(list(records))
            self._validate_import(snapshot)
            self._history.extend(snapshot)

    def build(self, run_id: str, real_user_text: str) -> tuple[Message, ...]:
        with self._lock:
            if run_id in self._completed:
                raise ValueError(f"Run {run_id!r} has already been committed")

            if self._pending is not None:
                pending_run_id, pending_text, _, pending_s0 = self._pending
                if pending_run_id == run_id:
                    if pending_text != real_user_text:
                        raise ValueError(f"Run {run_id!r} was built with different user text")
                    return tuple(deepcopy(pending_s0))
                raise RuntimeError(f"Run {pending_run_id!r} is still pending")

            system_message: Message = {
                "kind": "text",
                "role": "system",
                "content": self._system_prompt,
                "id": "system",
                "source": "fixed_prompt",
            }
            user_message: Message = {
                "kind": "text",
                "role": "user",
                "content": real_user_text,
                "id": f"{run_id}:user",
                "source": "real_user",
            }
            s0 = (system_message, *deepcopy(self._history), user_message)
            self._pending = (run_id, real_user_text, user_message, s0)
            return tuple(deepcopy(s0))

    def commit(self, run_id: str, result: RunResult) -> None:
        with self._lock:
            result_snapshot = self._snapshot_result(result)
            completed = self._completed.get(run_id)
            if completed is not None:
                if completed == result_snapshot:
                    return
                raise ValueError(f"Run {run_id!r} was committed with a different result")

            if self._pending is None or self._pending[0] != run_id:
                raise RuntimeError(f"Run {run_id!r} has no pending prompt")

            _, _, user_message, _ = self._pending
            if result_snapshot.dispatch_started:
                validated_delta = self._validate_delta(result_snapshot.history_delta)
                self._history.extend((deepcopy(user_message), *validated_delta))

            self._completed[run_id] = result_snapshot
            self._pending = None

    @staticmethod
    def _snapshot_result(result: RunResult) -> RunResult:
        snapshot = deepcopy(result)
        return RunResult(
            status=snapshot.status,
            history_delta=tuple(snapshot.history_delta),
            output=snapshot.output,
            error=snapshot.error,
            diagnostics=snapshot.diagnostics,
            dispatch_started=snapshot.dispatch_started,
        )

    @staticmethod
    def _validate_delta(history_delta: tuple[Message, ...]) -> tuple[Message, ...]:
        required_fields = ("kind", "id", "role", "source")
        for message in history_delta:
            if not isinstance(message, dict) or any(
                not isinstance(message.get(field), str) or not message[field]
                for field in required_fields
            ):
                raise ValueError("History delta must contain canonical messages.")
        return tuple(deepcopy(history_delta))

    @staticmethod
    def _validate_import(records: list[Message]) -> None:
        message_ids: set[str] = set()
        call_ids: set[str] = set()
        pending: list[tuple[str, str]] = []

        def pairs_to_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key in imported arguments")
                value[key] = item
            return value

        def reject_constant(value: str) -> None:
            raise ValueError("non-JSON constant in imported arguments")

        def same_json(left: Any, right: Any) -> bool:
            if type(left) is not type(right):
                return False
            if isinstance(left, dict):
                return left.keys() == right.keys() and all(same_json(left[key], right[key]) for key in left)
            if isinstance(left, list):
                return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
            return left == right

        for record in records:
            if not isinstance(record, dict):
                raise ValueError("imported history requires canonical messages")
            message_id = record.get("id")
            if not isinstance(message_id, str) or not message_id or message_id in message_ids:
                raise ValueError("duplicate or missing imported message ID")
            message_ids.add(message_id)
            kind = record.get("kind")
            role = record.get("role")
            source = record.get("source")
            if not isinstance(role, str) or not isinstance(source, str):
                raise ValueError("invalid imported role or source")
            if pending and kind != "tool_result":
                raise ValueError("imported tool calls have missing results")
            if kind == "text":
                if (role, source) not in {
                    ("user", "real_user"),
                    ("user", "protocol_feedback"),
                    ("assistant", "assistant_turn"),
                } or not isinstance(record.get("content"), str):
                    raise ValueError("invalid imported text message")
            elif kind == "assistant_calls":
                calls = record.get("calls")
                if (role, source) != ("assistant", "assistant_turn") or not isinstance(calls, list) or not calls:
                    raise ValueError("invalid imported call batch")
                if record.get("content") is not None and not isinstance(record["content"], str):
                    raise ValueError("invalid imported assistant content")
                names: list[str] = []
                for call in calls:
                    if not isinstance(call, dict):
                        raise ValueError("invalid imported call")
                    call_id, name, raw, arguments = (
                        call.get("id"), call.get("name"), call.get("raw_arguments"), call.get("arguments")
                    )
                    if (
                        not isinstance(call_id, str) or not call_id or call_id in call_ids
                        or not isinstance(name, str) or not name or not isinstance(raw, str)
                        or not isinstance(arguments, dict)
                    ):
                        raise ValueError("invalid imported call fields")
                    call_ids.add(call_id)
                    try:
                        parsed = json.loads(raw, object_pairs_hook=pairs_to_object, parse_constant=reject_constant)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("invalid imported raw arguments") from exc
                    if not isinstance(parsed, dict) or not same_json(parsed, arguments):
                        raise ValueError("imported raw and parsed arguments differ")
                    pending.append((call_id, name))
                    names.append(name)
                if "final_answer" in names and len(names) != 1:
                    raise ValueError("imported final must be exclusive")
            elif kind == "tool_result":
                if not pending:
                    raise ValueError("orphan imported tool result")
                call_id, name = pending.pop(0)
                status = record.get("status")
                if (
                    role != "tool" or record.get("tool_call_id") != call_id or record.get("name") != name
                    or record.get("source") != ("final_control" if name == "final_answer" else "tool_execution")
                    or not isinstance(status, str)
                    or status not in {"success", "error", "not_executed", "uncertain"}
                    or not isinstance(record.get("content"), str)
                ):
                    raise ValueError("imported tool result does not match its call")
            else:
                raise ValueError("unsupported imported history kind")
        if pending:
            raise ValueError("imported tool calls have missing results")
