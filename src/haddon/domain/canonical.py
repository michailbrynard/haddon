"""Canonical JSON and SHA-256 for content hashes (spec: approval hash binds schema version, entity/year, full
payload, evidence, dependencies and any replaced decision).  Amounts inside payloads are integer strings already.

This is also where ``entries.tags`` is canonicalised (chunk E1).  A transaction's tags are stored as canonical JSON,
an array of ``["name","value"]`` pairs, because that is the one representation that is exact in both directions.  A
tag has a name and a value, a transaction may carry the same name twice, and the order hledger reports is a
deterministic function of the transaction's comment.  Sorting or de-duplicating would lose information the export
has to put back, so the order is preserved and is part of the value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from haddon.domain.errors import validation

SCHEMA_VERSION = "ledger-proposal-1"

Tag = tuple[str, str]
NO_TAGS = "[]"
# hledger ends a tag name at a colon and separates tags with commas, so a name holding either (or whitespace)
# could not be written back into a comment and read as the same tag.  Refused rather than mangled.
_BAD_IN_TAG_NAME = re.compile(r"[\s:,]")
_BAD_IN_TAG_VALUE = ("\n", "\r")


def canonical_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise validation(f"payload is not canonical JSON: {exc}", reason="json_invalid") from None


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(**fields: Any) -> str:
    return sha256_hex(canonical_json({"schema": SCHEMA_VERSION, **fields}))


def check_tags(tags: Iterable[Any], *, path: str = "tags") -> tuple[Tag, ...]:
    """Validate a transaction's tags and return them as ``(name, value)`` pairs with the given order preserved."""
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        raise validation("tags must be a sequence of (name, value) pairs", path=path, reason="tags_shape")
    out: list[Tag] = []
    for i, pair in enumerate(tags):
        if isinstance(pair, (str, bytes)) or not isinstance(pair, Iterable):
            raise validation(f"{path}[{i}] is not a (name, value) pair", path=f"{path}[{i}]", reason="tag_shape")
        items = list(pair)
        if len(items) != 2 or not all(isinstance(x, str) for x in items):
            raise validation(f"{path}[{i}] is not a (name, value) pair of strings", path=f"{path}[{i}]", reason="tag_shape")
        name, value = items
        if not name or _BAD_IN_TAG_NAME.search(name):
            raise validation(f"tag name {name!r} is empty or holds whitespace, ':' or ','", path=f"{path}[{i}]",
                             reason="tag_name")
        for bad in _BAD_IN_TAG_VALUE:
            if bad in value:
                raise validation(f"tag {name!r} has a value containing {bad!r}, which a comment line cannot carry",
                                 path=f"{path}[{i}]", reason="tag_value")
        out.append((name, value))
    return tuple(out)


def canonical_tags(tags: Iterable[Any], *, path: str = "tags") -> str:
    """Return the stored form of ``entries.tags``: canonical JSON, order preserved, ``'[]'`` when there are none."""
    return canonical_json([[name, value] for name, value in check_tags(tags, path=path)])


def parse_tags(stored: str, *, path: str = "tags") -> tuple[Tag, ...]:
    """Turn the stored JSON back into ``(name, value)`` pairs; the inverse of :func:`canonical_tags`."""
    if not isinstance(stored, str):
        raise validation("tags column is not text", path=path, reason="tags_shape")
    try:
        raw = json.loads(stored)
    except json.JSONDecodeError as exc:
        raise validation(f"tags column is not JSON: {exc}", path=path, reason="json_invalid") from None
    if not isinstance(raw, list):
        raise validation("tags column is not a JSON array", path=path, reason="tags_shape")
    return check_tags(raw, path=path)
