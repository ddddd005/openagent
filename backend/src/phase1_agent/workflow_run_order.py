"""Workflow-owned run order declaration: the five built-in big run steps.

T6 E1 narrow module (142 号 §4.1/§4.2/§6): the workflow declares its built-in
run step order once here, validates it deterministically and exposes read-only
ordered views.  The workflow execution layer consumes the ascending result for
the real calls; the declaration input order never decides execution order.

It owns no business I/O, no database connection, no lease, no scheduler and no
mutable global registry: the built-in declaration is one immutable tuple and the
functions are pure reads.  ``entry_ref`` is a symbolic trusted-local reference
bound by the execution layer to its own closure/private method; it is never
interpreted as a module path and no module is imported dynamically.

Duplicate step identities and duplicate order values are explicit errors, never
resolved by sorting or overwriting.  The declared range 0-9000 leaves the
already-agreed three intervals available, but this first version registers only
the current built-in five steps and no extension protocol.
"""

from __future__ import annotations

from dataclasses import dataclass

STAGE_START = "start"
STAGE_BACKGROUND = "background"
STAGES = (STAGE_START, STAGE_BACKGROUND)

ORDER_MIN = 0
ORDER_MAX = 9000

_TEXT_FIELDS = ("step_id", "name", "entry_ref")


class RunOrderError(ValueError):
    """The run step declaration is invalid; it is never silently repaired."""


@dataclass(frozen=True, slots=True)
class RunStep:
    """One declared big run step: stable identity, order, name, entry and stage."""

    step_id: str
    order: int
    name: str
    entry_ref: str
    stage: str


BUILTIN_STEPS: tuple[RunStep, ...] = (
    RunStep("prepare", 100, "本轮准备", "graph.run.prepare", STAGE_START),
    RunStep("execute_graph", 1000, "整图执行", "graph.run.execute", STAGE_BACKGROUND),
    RunStep("settle", 2000, "业务结算", "graph.run.settle", STAGE_BACKGROUND),
    RunStep("notify", 2100, "提交后通知", "graph.run.notify", STAGE_BACKGROUND),
    RunStep("release", 2200, "结束释放", "graph.run.release", STAGE_BACKGROUND),
)


def _require_step(step):
    if not isinstance(step, RunStep):
        raise RunOrderError("Run step declaration must be a RunStep")
    for field in _TEXT_FIELDS:
        value = getattr(step, field)
        if type(value) is not str or not value:
            raise RunOrderError(f"Run step {field} must be a non-empty string")
    # bool is an int subclass, so it must be rejected explicitly.
    if type(step.order) is not int or isinstance(step.order, bool):
        raise RunOrderError("Run step order must be an integer")
    if not ORDER_MIN <= step.order <= ORDER_MAX:
        raise RunOrderError(
            f"Run step order {step.order} is outside {ORDER_MIN}..{ORDER_MAX}")
    if step.stage not in STAGES:
        raise RunOrderError(f"Unknown run step stage: {step.stage!r}")


def _validated_steps(steps):
    if type(steps) not in (tuple, list):
        raise RunOrderError("Run steps must be an ordered sequence")
    identities: set[str] = set()
    orders: set[int] = set()
    result: list[RunStep] = []
    for step in steps:
        _require_step(step)
        if step.step_id in identities:
            raise RunOrderError(f"Duplicate run step identity: {step.step_id}")
        if step.order in orders:
            raise RunOrderError(f"Duplicate run step order: {step.order}")
        identities.add(step.step_id)
        orders.add(step.order)
        result.append(step)
    return tuple(result)


def ordered_steps(steps=BUILTIN_STEPS) -> tuple[RunStep, ...]:
    """Validate every step, then return ascending ``order`` independent of input."""
    return tuple(sorted(_validated_steps(steps), key=lambda step: step.order))


def steps_for_stage(stage, steps=BUILTIN_STEPS) -> tuple[RunStep, ...]:
    """Return the validated ascending subset of one declared stage."""
    if stage not in STAGES:
        raise RunOrderError(f"Unknown run stage: {stage!r}")
    return tuple(step for step in ordered_steps(steps) if step.stage == stage)
