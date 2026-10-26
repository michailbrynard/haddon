"""The REST spine and read surface (chunk D1).

``haddon.api`` serves ``/v1`` over the library beneath it: entity-relative paths, one opaque actor per
credential, the domain error envelope, ``Idempotency-Key`` on every mutating verb, opaque snapshot-bound
cursors and date-range scoping.  It owns no accounting logic.  Every read goes through ``haddon.repository`` or
``haddon.reports``, and every write goes through ``repository.write_transaction``.

``create_app(config)`` builds the ASGI application (an empty config when none is given, useful for introspection
and tests); ``haddon.api.cli`` is ``bin/haddon-serve``.
"""

from haddon.api.app import create_app
from haddon.api.config import ApiConfig, Credential, load_config

__all__ = ["create_app", "ApiConfig", "Credential", "load_config"]
