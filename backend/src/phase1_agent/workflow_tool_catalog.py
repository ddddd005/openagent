"""Small trusted callable catalog shared by prompt declarations and Agent."""

from .tools import final_answer_tool, register_callable


def builtin_workflow_tools():
    def inspect_text(text: str) -> dict:
        return {"characters": len(text), "lines": len(text.splitlines())}

    return (register_callable("inspect_text", "Count characters and lines in a text.", {
        "type": "object", "properties": {"text": {"type": "string", "description": "Text to inspect"}},
        "required": ["text"], "additionalProperties": False,
    }, inspect_text), final_answer_tool())


def builtin_prompt_tool_catalog():
    return {(tool.name, "1"): tool for tool in builtin_workflow_tools()}
