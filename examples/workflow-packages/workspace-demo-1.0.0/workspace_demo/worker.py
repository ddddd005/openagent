"""Long-running JSON-lines workspace worker for the controlled demo package.

The worker is started by the trusted package adapter (``package.py``) with an
absolute state path.  It answers one JSON object per line and exits only after
a ``quit`` command.  ``finish`` writes the current state atomically; ``quit``
and ``abort`` never write.  The adapter owns process startup/termination; the
worker only implements the demo protocol.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

INITIAL_LOCATION = "city_gate"
PLACES = ("city_gate", "market_square", "harbor")
INITIAL_STATE = {"location": INITIAL_LOCATION, "moves": []}


def _initial_state() -> dict:
    return {"location": INITIAL_LOCATION, "moves": []}


def load_state(state_path: str) -> dict:
    """Load the last saved state; a missing file starts from the initial state."""
    path = Path(state_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _initial_state()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {"location", "moves"}
            or type(value["location"]) is not str or type(value["moves"]) is not list):
        raise ValueError("saved workspace state has an unexpected shape")
    moves = []
    for move in value["moves"]:
        if (type(move) is not dict or set(move) != {"from", "to"}
                or type(move["from"]) is not str or type(move["to"]) is not str):
            raise ValueError("saved workspace movement has an unexpected shape")
        moves.append({"from": move["from"], "to": move["to"]})
    return {"location": value["location"], "moves": moves}


def save_state(state_path: str, state: dict) -> None:
    """Replace the state file atomically so a crash never leaves it half written."""
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(
        json.dumps({"location": state["location"], "moves": state["moves"]},
                   ensure_ascii=True, sort_keys=True),
        encoding="utf-8")
    os.replace(temporary, target)


def export_text(state: dict) -> str:
    """The text the normal finish hands to the program for result composition."""
    moves = state["moves"]
    history = " -> ".join(
        [moves[0]["from"], *[move["to"] for move in moves]]) if moves else "(none)"
    return "current location: " + state["location"] + "\nmovement history: " + history


def look(state: dict) -> dict:
    return {"ok": True, "location": state["location"], "places": list(PLACES)}


def status(state: dict) -> dict:
    return {"ok": True, "location": state["location"], "moves": [dict(move) for move in state["moves"]],
            "move_count": len(state["moves"])}


def move(state: dict, arguments: dict) -> dict:
    destination = arguments.get("destination")
    if destination not in PLACES:
        return {"ok": False, "error": "unknown destination: " + str(destination)}
    origin = state["location"]
    state["location"] = destination
    state["moves"].append({"from": origin, "to": destination})
    return {"ok": True, "from": origin, "location": destination,
            "move_count": len(state["moves"])}


def finish(state: dict, state_path: str) -> dict:
    save_state(state_path, state)
    return {"ok": True, "text": export_text(state)}


def dispatch(message: dict, state: dict, state_path: str) -> tuple[dict, bool]:
    """Handle one command; the second value tells the loop whether to exit."""
    operation = message.get("op")
    arguments = message.get("arguments")
    if type(arguments) is not dict:
        arguments = {}
    if operation == "look":
        return look(state), False
    if operation == "status":
        return status(state), False
    if operation == "move":
        return move(state, arguments), False
    if operation == "finish":
        return finish(state, state_path), False
    if operation == "quit":
        return {"ok": True}, True
    return {"ok": False, "error": "unknown operation: " + str(operation)}, False


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("worker requires one state path argument\n")
        return 2
    state_path = argv[1]
    state = load_state(state_path)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            reply, should_exit = {"ok": False, "error": "invalid json command"}, False
        else:
            if type(message) is not dict:
                reply, should_exit = {"ok": False, "error": "command must be an object"}, False
            else:
                reply, should_exit = dispatch(message, state, state_path)
        sys.stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
        sys.stdout.flush()
        if should_exit:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
