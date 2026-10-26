"""``bin/haddon-serve``: run the REST service.

The app is built from a JSON configuration (``--config`` or ``$HADDON_CONFIG``) so that which entity lives in
which file, and which token acts as which actor, is deployment state rather than code.  ``--check`` builds the
app, opens every configured entity and prints the readiness report without binding a port.  A deploy should run
it before switching traffic over.
"""

from __future__ import annotations

import argparse
import json
import sys

from haddon.api.app import create_app
from haddon.api.config import load_config
from haddon.domain.errors import HaddonError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-serve", description="Serve the Haddon REST API.")
    parser.add_argument("--config", help="path to the JSON configuration (default: $HADDON_CONFIG)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true",
                        help="open every configured entity, print readiness and exit without serving")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except HaddonError as exc:
        print(f"configuration error: {exc.message}", file=sys.stderr)
        return 2
    app = create_app(config)
    if args.check:
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            response = client.get("/readyz")
        print(json.dumps(response.json(), indent=2, sort_keys=True))
        return 0 if response.status_code == 200 else 1
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
