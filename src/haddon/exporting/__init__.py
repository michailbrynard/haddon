"""Deterministic export of one entity's book back out of the store as hledger journal files (chunk C2a).

``render``    Pure rendering: a ``BookState`` in, ``{filename: text}`` out.  Fixes the transaction order (``dt``,
              then entry id), the account, commodity and file order, and the amount rendering (integer micros at
              the commodity's declared precision).  Refuses content a journal cannot carry back exactly.
``exporter``  Reads the posted entries of a date range, renders them, stages the files and runs
              ``hledger check --strict`` with the pinned binary, publishes and re-hashes the published bytes.  Then
              one ``write_transaction`` records the operation, the snapshot, the immutable ``exports`` row and the
              ``export.completed`` event.
``cli``       ``bin/haddon-export <db-path> <out-root>``.

Two exports of the same book are byte-identical, in this process and the next: nothing rendered comes from the
clock, a uuid, a float or a set iteration order.
"""
