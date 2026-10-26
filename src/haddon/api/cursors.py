"""Opaque pagination cursors.

A cursor is ``base64url(canonical_json(payload)) + "." + hmac_sha256(payload, secret)``.  The signature makes
"opaque" enforceable.  A caller that decodes a cursor, edits the entity, the filters, the snapshot or the position
and sends it back is refused, so no page can be widened, re-pointed at another entity or spliced onto a different
query.  Callers pass cursors back unchanged.

Per the spec, the payload binds the entity, the collection, a digest of the filters and the ordering, the
snapshot the listing was opened at, the watermark that bounds it, a probe of the covered rows and the position
reached.  ``decode`` returns the payload or raises ``422 validation`` with ``reason='cursor_invalid'``.  Whether a
valid cursor is still servable is decided in ``haddon.api.pagination``.

The payload also names the key that signed it (``kid``), so the signing secret can be rotated without
invalidating cursors in flight.  ``CursorKeyring.active`` signs, and verification looks up the named key among
those still configured (``haddon.api.config``).  Naming the key inside the signed body is safe because the name
only chooses a key.  A cursor that names a key it was not signed with fails the same comparison as a tampered
one.  A cursor naming a key that is no longer configured is refused, which is what makes retiring a key mean
something.  ``VERSION`` versions the payload's shape, not the key: bump it when the fields change, rotate a key
when the secret does.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from haddon.api.config import CursorKeyring
from haddon.domain.canonical import canonical_json
from haddon.domain.errors import validation

VERSION = 2


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encode(payload: dict[str, Any], keys: CursorKeyring) -> str:
    if "v" in payload or "kid" in payload:  # the envelope's own fields; a collision would silently unsign
        raise ValueError("a cursor payload may not carry 'v' or 'kid'")
    key = keys.active
    body = canonical_json({"v": VERSION, "kid": key.id, **payload}).encode("utf-8")
    return f"{_b64(body)}.{_sign(body, key.secret)}"


def decode(token: object, keys: CursorKeyring, *, path: str = "cursor") -> dict[str, Any]:
    """Return the payload of a cursor this service issued, or raise 422 ``cursor_invalid``."""
    bad = validation("the cursor is not one this service issued; restart the listing without it",
                     path=path, reason="cursor_invalid")
    if not isinstance(token, str) or token.count(".") != 1:
        raise bad
    encoded, signature = token.split(".", 1)
    try:
        body = _unb64(encoded)
        payload = json.loads(body.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise bad from None
    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        raise bad
    # The key id is read from the body before the body is trusted, but it only picks which secret the signature
    # is checked against.  An unknown key id gets the same refusal as a bad signature: a cursor signed by a
    # retired key is no longer one this service issued.
    key = keys.by_id(payload.get("kid"))
    if key is None or not hmac.compare_digest(_sign(body, key.secret), signature):
        raise bad
    return payload
