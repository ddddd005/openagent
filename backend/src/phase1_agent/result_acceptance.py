"""Private failure context for accepting an already completed result."""

from contextlib import contextmanager

from .contract_errors import ContractValidationError


@contextmanager
def persistence_boundary(stage):
    """Mark only an actual persistence operation after its contract checks."""
    try:
        yield
    except Exception as error:
        error._result_persistence_stage = stage
        raise


def retained_acceptance_allowed(error, *, stage, definite_result):
    if not definite_result or stage not in (
            "fact_acceptance", "result_acceptance", "result_projection",
            "node_acceptance", "round_acceptance"):
        return False
    if isinstance(error, ContractValidationError):
        return getattr(error, "_result_persistence_stage", None) == stage
    return True


def failure_responsibility(error):
    if getattr(error, "_result_persistence_stage", None) is not None:
        return "persistence"
    return ("projection" if getattr(error, "_result_failure_stage", None) == "result_projection"
            else "acceptance")
