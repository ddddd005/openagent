"""Logical-floor placement shared by ordinary and context assemblies."""

from .graph_contracts import require


LOGICAL_FLOOR_PLACEMENT = "logical-floors-v1"


def uses_logical_floor_placement(value):
    if "placement_profile" not in value:
        return False
    require(value["placement_profile"] == LOGICAL_FLOOR_PLACEMENT,
            "graph_prompt_placement_invalid", "Unknown prompt placement profile")
    return True


def logical_floor_materials(items, floor_starts, message_count):
    """Depth zero follows the current root; overflow clamps to the first edge."""
    offsets = [0, *floor_starts[1:], message_count] if floor_starts else [0]
    placed = []
    for index, item in enumerate(items):
        region = item["placement"]
        position = (offsets[max(0, len(offsets) - 1 - item["depth"])] if region == "middle"
                    else 0 if region == "before" else message_count)
        placed.append((position, {"before": 0, "middle": 1, "after": 2}[region],
                       item["order"], index, item))
    buckets = {}
    for position, _region, _order, _index, item in sorted(placed, key=lambda value: value[:4]):
        buckets.setdefault(position, []).append(item)
    return buckets
