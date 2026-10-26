"""Exact import of a corpus's hledger journals into a Haddon entity database (chunk C1).

``hledger``      The pinned-binary boundary.  Verifies ``vendor/hledger`` (version and sha256) before every run,
                 passes hledger's own stderr through on failure, and parses its JSON with strict shape checks.
                 Amounts are read from ``decimalMantissa`` / ``decimalPlaces`` only.
``importer``     One ``write_transaction`` per entity: the strict gate, the chart provisioned from what the journal
                 declares, then every transaction in the range posted in ``tindex`` order with its
                 ``journal_mappings`` row.  A journal that fails the gate gives a structured blocked result.
``corpus``       The manifest naming where the journals are and which entities and years they hold.
``restatement``  Verifies, instead of posting, an opening that restates a period the journal already holds.
``cli``          ``bin/haddon-import <Entity> <db-path>``.
"""
