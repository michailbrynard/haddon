"""The wire: paths, links, and the rule that amounts are strings in both directions.

Outbound, every amount is rendered with ``Micros.render``, a base-10 integer count of millionths.  Inbound,
``reject_wire_numbers`` walks the whole request body before anything else looks at it and refuses:

* a JSON float anywhere, whatever the key.  A float is meaningful nowhere in this contract, and accepting one
  somewhere harmless teaches callers to send them somewhere harmful;
* any ``*_micros`` value, or any decimal amount (``amount``, ``opening``, ``closing``), that is not a string.
  That includes a JSON integer.  ``12000000`` and ``"12000000"`` are not the same claim: the first has already
  passed through a JSON number type, and from nine significant digits up that type cannot always hold it.

``tests/test_no_float_reaches_db.py`` guards the same contract underneath; this is its HTTP end.
"""

from __future__ import annotations

from typing import Any

from haddon.domain.micros import Micros
from haddon.domain.errors import validation

V1 = "/v1"
MICROS_SUFFIX = "_micros"

#: The keys that carry a decimal amount in the published command surface (issue 0006).  A JSON number under one
#: of these gets the amount contract's own refusal naming the rule, not a pydantic "expected str".  ``1200`` under
#: ``opening`` is exactly the mistake the contract exists to catch, and a caller told "amounts are strings" fixes
#: the whole class of bug rather than the one field.
DECIMAL_KEYS = frozenset({"amount", "opening", "closing"})


def entity_base(entity_id: str) -> str:
    return f"{V1}/entities/{entity_id}"


def link(entity_id: str, suffix: str) -> str:
    return f"{entity_base(entity_id)}{suffix}"


def micros(value: Micros | int | None) -> str:
    if value is None:
        return "0"
    return value.render() if isinstance(value, Micros) else Micros(int(value)).render()


def micros_map(values: dict[str, Any]) -> dict[str, str]:
    return {code: micros(amount) for code, amount in sorted(values.items())}


def reject_wire_numbers(value: Any, *, path: str = "body") -> Any:
    """Refuse floats anywhere and non-string amounts on any ``*_micros`` key.  Returns the value unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise validation(f"{path}: amounts and every other value cross the wire as strings, never as JSON numbers "
                         f"with a fractional part", path=path, reason="float_rejected")
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if isinstance(key, str) and key.endswith(MICROS_SUFFIX):
                Micros.parse(item, path=child)  # 422 for a JSON number, a float, or a non-canonical string
            elif key in DECIMAL_KEYS and not isinstance(item, str):
                raise validation(f"{child}: amounts are decimal strings, never JSON numbers",
                                 path=child, reason="string_required", type=type(item).__name__)
            reject_wire_numbers(item, path=child)
        return value
    if isinstance(value, list):
        for i, item in enumerate(value):
            reject_wire_numbers(item, path=f"{path}[{i}]")
        return value
    return value


def scope_fields(entity: dict[str, str], snapshot_seq: int) -> dict[str, Any]:
    """Return the entity identity fields every entity-scoped response carries."""
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": snapshot_seq}


def parse_body(raw: bytes, model):
    """Turn request bytes into a validated submission: JSON, then the amount contract, then the published schema.

    ``reject_wire_numbers`` runs before the model, so a JSON number in an amount gets the amount contract's own
    refusal naming the rule, not a pydantic type error about strings.
    """
    import json

    from pydantic import ValidationError

    from haddon.domain.errors import HaddonError

    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise validation(f"the request body is not JSON: {exc}", path="body", reason="json_invalid") from None
    if not isinstance(data, dict):
        raise validation("the request body must be a JSON object", path="body", reason="object_required")
    reject_wire_numbers(data)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = [{"path": ".".join(str(p) for p in e.get("loc", ())), "reason": e.get("type", "validation"),
                    "message": e.get("msg", "")} for e in exc.errors()]
        raise HaddonError("validation", "the request does not match the published schema",
                          details=details) from None
