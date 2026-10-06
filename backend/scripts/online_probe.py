"""Explicit, bounded DeepSeek acceptance probe. Never logs prompts or credentials."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from smolagents import Tool

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.context import PromptContext
from phase1_agent.contracts import ModelResponse, RunInput, RunLimits
from phase1_agent.kernel import AgentKernel
from phase1_agent.tools import final_answer_tool, register_tool


MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
MAX_TOKENS = 256
MAX_HTTP_ATTEMPTS = 16
OUTPUT_SPEC = {
    "type": "object",
    "required": ["content"],
    "properties": {"content": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
CORRECTION = "请只调用 final_answer，answer 必须是含非空 content 字符串的 JSON 对象。"


class CappedTransport(httpx.BaseTransport):
    def __init__(self, max_attempts: int = MAX_HTTP_ATTEMPTS, delegate: httpx.BaseTransport | None = None):
        self.max_attempts = max_attempts
        self.attempts = 0
        self._delegate = delegate or httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != "api.deepseek.com":
            raise RuntimeError("probe only permits the configured DeepSeek HTTPS endpoint")
        if self.attempts >= self.max_attempts:
            raise RuntimeError("HTTP attempt budget exhausted")
        self.attempts += 1
        return self._delegate.handle_request(request)

    def close(self) -> None:
        self._delegate.close()


class ReadNote(Tool):
    name = "read_note"
    description = "Read a note from the current node's in-memory store."
    inputs = {"path": {"type": "string", "description": "Path of the note to read"}}
    output_type = "string"

    def __init__(self, store: dict[str, str]):
        super().__init__()
        self.store = store
        self.calls = 0

    def forward(self, path: str) -> str:
        self.calls += 1
        return self.store[path]


class WriteNote(Tool):
    name = "write_note"
    description = "Write a note to the current node's in-memory store."
    inputs = {
        "path": {"type": "string", "description": "Path of the note to write"},
        "text": {"type": "string", "description": "Text to write"},
    }
    output_type = "string"

    def __init__(self, store: dict[str, str]):
        super().__init__()
        self.store = store
        self.calls = 0

    def forward(self, path: str, text: str) -> str:
        self.calls += 1
        self.store[path] = text
        return "written"


class ObservingAdapter:
    def __init__(self, adapter: DeepSeekAdapter):
        self._adapter = adapter
        self.events: list[dict[str, Any]] = []

    def generate(self, messages, tools) -> ModelResponse:
        shape = {
            "roles": [message["role"] for message in messages],
            "tool_call_ids": [
                call["id"]
                for message in messages
                if message.get("kind") == "assistant_calls"
                for call in message["calls"]
            ],
            "result_ids": [
                message["tool_call_id"] for message in messages if message.get("kind") == "tool_result"
            ],
        }
        response = self._adapter.generate(messages, tools)
        self.events.append(
            {
                "request": shape,
                "response_id": response.response_id,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "returned_call_names": [call.name for call in response.tool_calls],
                "returned_call_ids": [call.id for call in response.tool_calls],
                "usage": response.usage,
            }
        )
        return response


def run_probe(key_file: Path) -> dict[str, Any]:
    data = json.loads(key_file.read_text(encoding="utf-8"))
    key = data.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key file must contain a nonempty api_key string")

    transport = CappedTransport()
    report: dict[str, Any] = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model_requested": MODEL,
        "endpoint": BASE_URL,
        "max_tokens_per_request": MAX_TOKENS,
        "max_http_attempts": MAX_HTTP_ATTEMPTS,
        "sdk_version": version("openai"),
    }
    with httpx.Client(transport=transport) as http_client:
        text_client = OpenAI(
            api_key=key, base_url=BASE_URL, http_client=http_client, max_retries=0, timeout=60
        )
        try:
            text_response = text_client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": "请只回复 OK。"}],
                stream=False,
                max_tokens=32,
                extra_body={"thinking": {"type": "disabled"}},
            )
            text_choice = text_response.choices[0]
            report["text_probe"] = {
                "response_id": text_response.id,
                "model": text_response.model,
                "finish_reason": text_choice.finish_reason,
                "has_content": bool(text_choice.message.content),
                "usage": text_response.usage.model_dump(exclude_none=True) if text_response.usage else None,
            }
        except Exception as exc:
            report["text_probe"] = {"error_type": type(exc).__name__, "stage": "text_request"}
            report["http_attempts"] = transport.attempts
            return report

        store = {"notes.txt": "草稿正文"}
        reader, writer = ReadNote(store), WriteNote(store)
        tools = (register_tool(reader), register_tool(writer), final_answer_tool())
        adapter = ObservingAdapter(
            DeepSeekAdapter(api_key=key, model=MODEL, base_url=BASE_URL, http_client=http_client, max_tokens=MAX_TOKENS)
        )
        context = PromptContext(
            "你是单节点测试代理。处理首条用户请求时先调用 read_note 读取 notes.txt，"
            "再调用 write_note 将读到的文本写入 draft.txt。拿到工具结果后，"
            "只用 final_answer({\"answer\":{\"content\":...}}) 输出 JSON 对象。"
            "下一条真实用户请求也使用 final_answer。final_answer 不得与普通工具混在同一批。"
        )

        first_s0 = context.build("online-1", "请读取 notes.txt，写入 draft.txt，最后返回写入内容。")
        first = AgentKernel().run(
            RunInput(
                run_id="online-1",
                s0=first_s0,
                tools=tools,
                adapter=adapter,
                output_spec=OUTPUT_SPEC,
                correction_message=CORRECTION,
                limits=RunLimits(max_model_requests=5),
            )
        )
        context.commit("online-1", first)
        report["first_run"] = {
            "status": first.status,
            "error": first.error,
            "valid_json_output": bool(first.output and isinstance(json.loads(first.output).get("content"), str)),
            "read_calls": reader.calls,
            "write_calls": writer.calls,
            "draft_written": store.get("draft.txt") == "草稿正文",
            "history_delta_count": len(first.history_delta),
        }
        if first.status == "success" and reader.calls and writer.calls:
            next_s0 = context.build("online-2", "请将第二稿作为 content 字段，用 final_answer 返回。")
            second = AgentKernel().run(
                RunInput(
                    run_id="online-2",
                    s0=next_s0,
                    tools=tools,
                    adapter=adapter,
                    output_spec=OUTPUT_SPEC,
                    correction_message=CORRECTION,
                    limits=RunLimits(max_model_requests=2),
                )
            )
            context.commit("online-2", second)
            report["second_run"] = {
                "status": second.status,
                "error": second.error,
                "valid_json_output": bool(second.output and isinstance(json.loads(second.output).get("content"), str)),
                "s0_history_count": len(next_s0) - 2,
            }
        report["model_events"] = adapter.events
        report["http_attempts"] = transport.attempts
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded, redacted DeepSeek acceptance probes")
    parser.add_argument("--key-file", type=Path, required=True, help="JSON file containing api_key")
    parser.add_argument("--execute", action="store_true", help="Required to allow network requests")
    args = parser.parse_args()
    if not args.execute:
        parser.error("add --execute to permit the bounded online probe")
    try:
        report = run_probe(args.key_file)
    except Exception as exc:
        report = {"error_type": type(exc).__name__, "stage": "probe"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("second_run", {}).get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
