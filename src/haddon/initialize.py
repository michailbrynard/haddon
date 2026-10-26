"""``bin/haddon-init <db-path> --id ... --name ...``: migrate a fresh file and initialize a generic entity.

This is the generic bootstrap issue 0006 asks for: no source checkout, no import year and no entity list baked
into the product.  All of the logic is :func:`haddon.commands.initialize_entity`; this is the thin command line
over it.  The chart is the starter chart from :mod:`haddon.commands` unless ``--no-starter-chart`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys

from haddon.commands import STARTER_CHART, initialize_entity
from haddon.db.connection import make_engine
from haddon.db.migrate import upgrade
from haddon.repository.transaction import write_transaction

DEFAULT_ACTOR = "cli:haddon-init"


def _commodity(text: str) -> tuple[str, int]:
    if ":" not in text:
        raise argparse.ArgumentTypeError("commodity must be CODE:PRECISION, e.g. USD:2")
    code, _, precision = text.partition(":")
    try:
        return code.upper(), int(precision)
    except ValueError:
        raise argparse.ArgumentTypeError(f"precision {precision!r} is not an integer") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-init",
                                     description="Migrate a fresh database file and initialize a generic entity.")
    parser.add_argument("db_path")
    parser.add_argument("--id", required=True, help="stable entity id (no spaces)")
    parser.add_argument("--name", required=True)
    parser.add_argument("--legal-form", required=True, help="e.g. Ltd, Inc, B.V.")
    parser.add_argument("--country", required=True, help="ISO country code, recorded on the entity")
    parser.add_argument("--base-currency", required=True)
    parser.add_argument("--base-precision", required=True, type=int,
                        help="decimal places for the base currency, e.g. 2 for USD or 0 for JPY")
    parser.add_argument("--commodity", type=_commodity, action="append", default=[],
                        help="additional CODE:PRECISION beyond the base currency (repeatable)")
    parser.add_argument("--actor", default=DEFAULT_ACTOR, help="the acting principal recorded on the audit action")
    parser.add_argument("--no-starter-chart", action="store_true", help="do not add the starter chart")
    args = parser.parse_args(argv)

    upgrade(args.db_path)
    engine = make_engine(args.db_path)
    try:
        with write_transaction(engine) as conn:
            result = initialize_entity(conn, actor=args.actor, id=args.id, name=args.name,
                                       legal_form=args.legal_form, country=args.country,
                                       base_currency=args.base_currency, base_precision=args.base_precision,
                                       commodities=tuple(args.commodity),
                                       chart=() if args.no_starter_chart else STARTER_CHART)
    finally:
        engine.dispose()
    print(json.dumps(result.as_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
