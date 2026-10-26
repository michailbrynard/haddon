"""SQLAlchemy Core ``Table`` objects for the tables the repositories read and write.

These mirror the migrated schema column for column.  The types here are nominal.  SQLite STRICT enforces storage
classes, and every CHECK, foreign key and trigger lives in the database.  ``tests/test_tables_match_schema.py`` fails
if a declaration drifts from the migrated schema, so this file cannot become a second, divergent source of truth.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

T, I = sa.Text, sa.Integer


def _common(*extra: sa.Column) -> list[sa.Column]:
    return [sa.Column("id", T, primary_key=True), sa.Column("entity_id", T, nullable=False),
            sa.Column("created_at", T, nullable=False), sa.Column("created_by", T, nullable=False), *extra]


entities = sa.Table(
    "entities", metadata,
    sa.Column("id", T, primary_key=True), sa.Column("singleton", I, nullable=False),
    sa.Column("name", T, nullable=False), sa.Column("legal_form", T, nullable=False), sa.Column("country", T, nullable=False),
    sa.Column("base_currency", T, nullable=False),
    sa.Column("tax_regime", T, nullable=False),
    sa.Column("created_at", T, nullable=False), sa.Column("created_by", T, nullable=False),
)
commodities = sa.Table("commodities", metadata, *_common(
    sa.Column("code", T, nullable=False), sa.Column("precision", I, nullable=False), sa.Column("quantum", I, nullable=False)))
accounts = sa.Table("accounts", metadata, *_common(
    sa.Column("path", T, nullable=False), sa.Column("type", T, nullable=False), sa.Column("code", T), sa.Column("tax_code", T),
    sa.Column("is_cash", I, nullable=False), sa.Column("archived", I, nullable=False), sa.Column("version", I, nullable=False)))
account_commodities = sa.Table("account_commodities", metadata, *_common(
    sa.Column("account_id", T, nullable=False), sa.Column("commodity", T, nullable=False)))
entries = sa.Table("entries", metadata, *_common(
    sa.Column("dt", T, nullable=False), sa.Column("narration", T, nullable=False),
    sa.Column("status", T, nullable=False), sa.Column("origin", T, nullable=False), sa.Column("version", I, nullable=False),
    # Migration 0002 (chunk E1): the transaction-level note fields, in the order ALTER TABLE appended them.
    sa.Column("code", T, nullable=False), sa.Column("comment", T, nullable=False), sa.Column("tags", T, nullable=False)))
postings = sa.Table("postings", metadata, *_common(
    sa.Column("entry_id", T, nullable=False), sa.Column("seq", I, nullable=False), sa.Column("account_id", T, nullable=False),
    sa.Column("commodity", T, nullable=False), sa.Column("amount_micros", I, nullable=False),
    sa.Column("memo", T), sa.Column("tax_code", T)))
corrections = sa.Table("corrections", metadata, *_common(
    sa.Column("original_id", T, nullable=False), sa.Column("mirror_id", T, nullable=False),
    sa.Column("replacement_id", T)))
snapshots = sa.Table("snapshots", metadata, *_common(
    sa.Column("snapshot_seq", I, nullable=False), sa.Column("state_hash", T, nullable=False),
    sa.Column("through", T, nullable=False), sa.Column("manifest", T, nullable=False), sa.Column("report", T, nullable=False),
    sa.Column("checks", T, nullable=False)))
exports = sa.Table("exports", metadata, *_common(
    sa.Column("snapshot_id", T, nullable=False), sa.Column("operation_id", T, nullable=False),
    sa.Column("manifest_sha256", T, nullable=False), sa.Column("artifacts", T, nullable=False),
    sa.Column("verifier", T, nullable=False)))
close_records = sa.Table("close_records", metadata, *_common(
    sa.Column("through", T, nullable=False), sa.Column("prior_through", T), sa.Column("snapshot_id", T, nullable=False),
    sa.Column("kind", T, nullable=False), sa.Column("reason", T, nullable=False),
    # Issue 0005: the gates this close was let through despite a real finding, each with the gate's status and
    # outstanding count at the time, the actor and the reason.  A reopen waives nothing and stores ``'[]'``.
    sa.Column("overrides", T, nullable=False)))
period_locks = sa.Table("period_locks", metadata, *_common(
    sa.Column("singleton", I, nullable=False), sa.Column("through", T), sa.Column("version", I, nullable=False),
    sa.Column("close_id", T)))
events = sa.Table(
    "events", metadata,
    sa.Column("seq", I, primary_key=True, autoincrement=True), sa.Column("entity_id", T, nullable=False),
    sa.Column("created_at", T, nullable=False), sa.Column("created_by", T, nullable=False),
    sa.Column("kind", T, nullable=False), sa.Column("subject_kind", T, nullable=False), sa.Column("subject_id", T, nullable=False),
    sa.Column("before", T), sa.Column("after", T), sa.Column("reason", T), sa.Column("request_id", T),
)
requests = sa.Table("requests", metadata, *_common(
    sa.Column("caller", T, nullable=False), sa.Column("intent_key", T, nullable=False), sa.Column("method", T, nullable=False),
    sa.Column("path", T, nullable=False), sa.Column("request_hash", T, nullable=False), sa.Column("http_status", I, nullable=False),
    sa.Column("response", T, nullable=False), sa.Column("operation_id", T)))
operations = sa.Table("operations", metadata, *_common(
    sa.Column("kind", T, nullable=False), sa.Column("state", T, nullable=False), sa.Column("intent", T, nullable=False),
    sa.Column("counts", T, nullable=False), sa.Column("errors", T, nullable=False), sa.Column("result", T),
    sa.Column("lease_until", T), sa.Column("attempts", I, nullable=False), sa.Column("version", I, nullable=False)))
journal_mappings = sa.Table("journal_mappings", metadata, *_common(
    sa.Column("source_revision", T, nullable=False),
    sa.Column("journal_path", T, nullable=False), sa.Column("source_index", I, nullable=False),
    sa.Column("source_status", T, nullable=False), sa.Column("canonical_txn", T, nullable=False),
    sa.Column("entry_id", T, nullable=False), sa.Column("txn_id", T)))

# The reconciliation extension (issue 0004, migration 0009).  Five records: the document, the statement it prints,
# that statement's lines, an append-only explanation per line, and the allocations one explanation makes against
# postings.  "Live" is derived, not stored.  A live statement is one nothing replaces and a live explanation is one
# nothing supersedes, so there is one notion of current, in SQL.
documents = sa.Table("documents", metadata, *_common(
    sa.Column("sha256", T, nullable=False), sa.Column("filename", T, nullable=False),
    sa.Column("media_type", T, nullable=False), sa.Column("size_bytes", I, nullable=False),
    sa.Column("blob_path", T, nullable=False), sa.Column("external_ref", T)))
statements = sa.Table("statements", metadata, *_common(
    sa.Column("account_id", T, nullable=False), sa.Column("commodity", T, nullable=False),
    sa.Column("period_start", T, nullable=False), sa.Column("period_end", T, nullable=False),
    sa.Column("opening_micros", I, nullable=False), sa.Column("closing_micros", I, nullable=False),
    sa.Column("document_id", T, nullable=False), sa.Column("page", I), sa.Column("locator", T, nullable=False),
    sa.Column("reason", T, nullable=False), sa.Column("replaces_id", T)))
statement_lines = sa.Table("statement_lines", metadata, *_common(
    sa.Column("statement_id", T, nullable=False), sa.Column("line_no", I, nullable=False),
    sa.Column("source_key", T, nullable=False), sa.Column("content_hash", T, nullable=False),
    sa.Column("dt", T, nullable=False), sa.Column("amount_micros", I, nullable=False),
    sa.Column("description", T, nullable=False), sa.Column("reference", T),
    sa.Column("fields", T, nullable=False), sa.Column("raw", T, nullable=False)))
explanations = sa.Table("explanations", metadata, *_common(
    sa.Column("line_id", T, nullable=False), sa.Column("kind", T, nullable=False),
    sa.Column("reason", T, nullable=False), sa.Column("entry_id", T), sa.Column("correction_id", T),
    sa.Column("supersedes_id", T)))
allocations = sa.Table("allocations", metadata, *_common(
    sa.Column("explanation_id", T, nullable=False), sa.Column("line_id", T, nullable=False),
    sa.Column("posting_id", T, nullable=False), sa.Column("amount_micros", I, nullable=False)))
evidence_links = sa.Table("evidence_links", metadata, *_common(
    sa.Column("document_id", T, nullable=False), sa.Column("entry_id", T), sa.Column("statement_id", T),
    sa.Column("page", I), sa.Column("locator", T, nullable=False), sa.Column("caption", T, nullable=False)))


# Migration 0009 derives "live" in SQL (``live_statements``, ``live_explanations``, ``live_allocations``).  These
# two functions are the same derivation for Python readers, written once so a reader cannot quietly disagree with
# the database about what "current" means.  There used to be three Python versions of "live explanation", and only
# one excluded a replaced statement, so a close check could count an allocation the database had already retired.
def live_statement_clause(table=None):
    """The ``live_statements`` view as a WHERE clause: no statement replaces this one."""
    table = statements if table is None else table
    successor = statements.alias("live_successor")
    return ~sa.exists().where(sa.and_(successor.c.entity_id == table.c.entity_id,
                                      successor.c.replaces_id == table.c.id))


def live_explanation_clause(entity_id: str, table=None):
    """The ``live_explanations`` view as a WHERE clause: nothing supersedes it, and its statement is live.

    The second half is easy to forget and expensive to get wrong.  Replacing a statement retires its lines'
    explanations, so the allocations underneath stop consuming their postings.  A reader that only checked
    ``supersedes_id`` would keep counting them and report a posting as over- or partly allocated.

    Both halves correlate only to ``explanations``.  The line is reached through its own alias, so this composes
    into a query that already joins ``statement_lines`` or ``statements`` without silently binding to them.
    """
    table = explanations if table is None else table
    later = explanations.alias("live_later")
    line = statement_lines.alias("live_line")
    replacing = statements.alias("live_replacing")
    return sa.and_(
        ~sa.exists().where(sa.and_(later.c.entity_id == entity_id, later.c.supersedes_id == table.c.id)),
        sa.exists().where(sa.and_(
            line.c.entity_id == entity_id, line.c.id == table.c.line_id,
            ~sa.exists().where(sa.and_(replacing.c.entity_id == entity_id,
                                       replacing.c.replaces_id == line.c.statement_id)))))

#: "Booked, awaiting a document" (docs/design/decisions.md).  Append-only with a ``supersedes_id`` chain, the same
#: shape ``explanations`` uses.  A posted entry is immutable, so a provisional judgement about one lives beside it.
document_expectations = sa.Table("document_expectations", metadata, *_common(
    sa.Column("entry_id", T, nullable=False), sa.Column("state", T, nullable=False),
    sa.Column("awaiting", T, nullable=False), sa.Column("reason", T, nullable=False),
    sa.Column("evidence_id", T), sa.Column("supersedes_id", T)))
#: Recoverable tax that was not recovered, and why (docs/design/decisions.md).  Append-only like
#: ``explanations`` and ``document_expectations``.  The amount is positive because it is the amount not recovered.
#: Nothing here names a country or a tax.
unrecovered_tax = sa.Table("unrecovered_tax", metadata, *_common(
    sa.Column("entry_id", T, nullable=False), sa.Column("posting_id", T),
    sa.Column("commodity", T, nullable=False), sa.Column("amount_micros", I, nullable=False),
    sa.Column("state", T, nullable=False), sa.Column("tax_code", T),
    sa.Column("reason", T, nullable=False), sa.Column("recoverable_until", T),
    sa.Column("recovered_entry_id", T), sa.Column("supersedes_id", T)))
