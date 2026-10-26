-- Ledger schema, migration 0001.  One file, one shape: this is the whole schema as it stands, not a
-- starting point plus nine corrections.
--
-- It was not retyped.  It is the `sqlite_master` text of a database built by running the old 0001..0010
-- chain to head, which is why the explanatory comments inside the table bodies survived the squash intact.
-- A few of them still say "migration 0002" or "migration 0005"; those numbers refer to the pre-squash
-- history, which is in git, not to any file that exists now.
--
-- This file is frozen: migration 0001 pins its sha256.  Schema changes go in a new numbered directory and a
-- new Alembic revision.
--
-- Conventions (spec): every non-entities table carries entity_id/created_at/created_by, UNIQUE(entity_id,id)
-- and composite (entity_id,ref) foreign keys; dates are TEXT 'YYYY-MM-DD' checked with date(v,'+0 days') IS v;
-- amounts are INTEGER micros with -2^63 excluded on EVERY amount column (decisions.md U6).
-- Under-specification resolutions from the review are marked "-- U<n>:" and listed in docs/decisions.md.
--
-- Order matters and is the order the old chain left behind: tables first, then the indexes over them, then
-- the views (live_explanations reads live_statements, and live_allocations reads live_explanations, so they
-- have to be declared in that order).

CREATE TABLE entities (
  id TEXT PRIMARY KEY,
  singleton INTEGER NOT NULL UNIQUE CHECK(singleton=1),
  name TEXT NOT NULL, legal_form TEXT NOT NULL, country TEXT NOT NULL, base_currency TEXT NOT NULL,
  tax_regime TEXT NOT NULL CHECK(json_valid(tax_regime)),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL
) STRICT;

CREATE TABLE commodities (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  code TEXT NOT NULL UNIQUE,
  precision INTEGER NOT NULL CHECK(precision BETWEEN 0 AND 6),
  quantum INTEGER NOT NULL CHECK(quantum = CASE precision WHEN 0 THEN 1000000 WHEN 1 THEN 100000 WHEN 2 THEN 10000 WHEN 3 THEN 1000 WHEN 4 THEN 100 WHEN 5 THEN 10 ELSE 1 END),
  UNIQUE(entity_id,id), UNIQUE(entity_id,code)
) STRICT;

CREATE TABLE accounts (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL CHECK(type IN ('asset','liability','equity','income','expense')),
  code TEXT, tax_code TEXT,
  is_cash INTEGER NOT NULL CHECK(is_cash IN (0,1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(entity_id,id)
) STRICT;

CREATE TABLE account_commodities (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  account_id TEXT NOT NULL, commodity TEXT NOT NULL,
  UNIQUE(entity_id,id), UNIQUE(entity_id,account_id,commodity),
  FOREIGN KEY(entity_id,account_id) REFERENCES accounts(entity_id,id),
  FOREIGN KEY(entity_id,commodity) REFERENCES commodities(entity_id,code)
) STRICT;

CREATE TABLE postings (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  entry_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq>0),
  account_id TEXT NOT NULL, commodity TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(amount_micros > -9223372036854775808),
  memo TEXT, tax_code TEXT,
  UNIQUE(entity_id,id), UNIQUE(entry_id,seq),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,account_id) REFERENCES accounts(entity_id,id),
  -- U1: spec writes FK(account_id,commodity)->account_commodities, whose only matching UNIQUE is
  -- (entity_id,account_id,commodity); the three-column form is the only well-formed reading.
  FOREIGN KEY(entity_id,account_id,commodity) REFERENCES account_commodities(entity_id,account_id,commodity)
) STRICT;

CREATE TABLE operations (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  kind TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed')),
  intent TEXT NOT NULL CHECK(json_valid(intent)), counts TEXT NOT NULL CHECK(json_valid(counts)),
  errors TEXT NOT NULL CHECK(json_valid(errors)), result TEXT CHECK(result IS NULL OR json_valid(result)),
  lease_until TEXT, attempts INTEGER NOT NULL, version INTEGER NOT NULL,
  UNIQUE(entity_id,id)
) STRICT;

CREATE TABLE requests (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  caller TEXT NOT NULL, intent_key TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
  request_hash TEXT NOT NULL, http_status INTEGER NOT NULL, response TEXT NOT NULL CHECK(json_valid(response)),
  operation_id TEXT,
  UNIQUE(entity_id,id), UNIQUE(entity_id,caller,intent_key),
  FOREIGN KEY(entity_id,operation_id) REFERENCES operations(entity_id,id)
) STRICT;

CREATE TABLE exports (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  snapshot_id TEXT NOT NULL, operation_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
  artifacts TEXT NOT NULL CHECK(json_valid(artifacts)), verifier TEXT NOT NULL CHECK(json_valid(verifier)),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,snapshot_id) REFERENCES snapshots(entity_id,id),
  FOREIGN KEY(entity_id,operation_id) REFERENCES operations(entity_id,id)
) STRICT;

CREATE TABLE period_locks (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  singleton INTEGER NOT NULL UNIQUE CHECK(singleton=1),
  through TEXT CHECK(through IS NULL OR date(through,'+0 days') IS through),
  version INTEGER NOT NULL DEFAULT 1, close_id TEXT,
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,close_id) REFERENCES close_records(entity_id,id)
) STRICT;

CREATE TABLE entries (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  dt TEXT NOT NULL CHECK(date(dt,'+0 days') IS dt),
  narration TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted','void')),
  origin TEXT NOT NULL CHECK(origin IN ('source','manual','agent','reversal','opening','invoice','payroll','adjustment')),
  version INTEGER NOT NULL DEFAULT 1,
  code TEXT NOT NULL DEFAULT '',  -- migration 0002 (chunk E1): hledger tcode
  comment TEXT NOT NULL DEFAULT '',  -- hledger tcomment, verbatim
  tags TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags)),  -- hledger ttags, canonical JSON, order preserved
  UNIQUE(entity_id,id)
) STRICT;

CREATE TABLE snapshots (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  snapshot_seq INTEGER NOT NULL, state_hash TEXT NOT NULL UNIQUE,
  through TEXT NOT NULL CHECK(date(through,'+0 days') IS through),
  manifest TEXT NOT NULL CHECK(json_valid(manifest)), report TEXT NOT NULL CHECK(json_valid(report)),
  checks TEXT NOT NULL CHECK(json_valid(checks)),
  UNIQUE(entity_id,id)
) STRICT;

CREATE TABLE documents (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL, media_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
  blob_path TEXT NOT NULL, external_ref TEXT,
  -- bytes we hold, or a reference to bytes somebody else holds -- exactly one, never neither and never both,
  -- so "where is the original?" always has one answer.
  CHECK((length(blob_path)>0)+(external_ref IS NOT NULL AND length(external_ref)>0)=1),
  UNIQUE(entity_id,id)
) STRICT;

CREATE TABLE statements (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  account_id TEXT NOT NULL, commodity TEXT NOT NULL,
  period_start TEXT NOT NULL CHECK(date(period_start,'+0 days') IS period_start),
  period_end TEXT NOT NULL CHECK(date(period_end,'+0 days') IS period_end),
  opening_micros INTEGER NOT NULL CHECK(opening_micros > -9223372036854775808),
  closing_micros INTEGER NOT NULL CHECK(closing_micros > -9223372036854775808),
  document_id TEXT NOT NULL, page INTEGER CHECK(page IS NULL OR page>0), locator TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  replaces_id TEXT UNIQUE,
  CHECK(period_start<=period_end),
  -- One document is one LIVE statement, which is `statements_insert`'s `document_already_loaded` rather than a
  -- key: "load it twice and double the period's lines" is still impossible, but a document whose statement has
  -- been REPLACED is free again, so the same download can be re-read and resubmitted when the first reading of
  -- it was wrong.  A UNIQUE(document_id) here said "one statement, ever" and made that correction -- the
  -- commonest one there is -- require different bytes or a faked hash (docs/decisions.md, issue 0004).
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,document_id) REFERENCES documents(entity_id,id),
  FOREIGN KEY(entity_id,account_id) REFERENCES accounts(entity_id,id),
  FOREIGN KEY(entity_id,commodity) REFERENCES commodities(entity_id,code),
  FOREIGN KEY(entity_id,account_id,commodity) REFERENCES account_commodities(entity_id,account_id,commodity),
  FOREIGN KEY(entity_id,replaces_id) REFERENCES statements(entity_id,id)
) STRICT;

CREATE TABLE statement_lines (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  line_no INTEGER NOT NULL CHECK(line_no>0),
  source_key TEXT NOT NULL CHECK(length(source_key)>0), content_hash TEXT NOT NULL,
  dt TEXT NOT NULL CHECK(date(dt,'+0 days') IS dt),
  amount_micros INTEGER NOT NULL CHECK(amount_micros<>0 AND amount_micros > -9223372036854775808),
  description TEXT NOT NULL, reference TEXT,
  fields TEXT NOT NULL CHECK(json_valid(fields)), raw TEXT NOT NULL,
  -- Identity is scoped to the statement, not the entity: a replacement statement re-states the SAME lines, and
  -- an entity-wide hash key would make correcting a mistaken load impossible.
  UNIQUE(entity_id,id), UNIQUE(statement_id,line_no), UNIQUE(statement_id,source_key),
  FOREIGN KEY(entity_id,statement_id) REFERENCES statements(entity_id,id)
) STRICT;

CREATE TABLE explanations (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  line_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('booked','linked','ignored')),
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  entry_id TEXT, correction_id TEXT, supersedes_id TEXT UNIQUE,
  -- `booked` posted the entry it names; `linked` points at postings somebody else wrote; `ignored` books
  -- nothing and says why.  The allocations hang off the first two.
  CHECK((kind='booked')=(entry_id IS NOT NULL)),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,line_id) REFERENCES statement_lines(entity_id,id),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,correction_id) REFERENCES corrections(entity_id,id),
  FOREIGN KEY(entity_id,supersedes_id) REFERENCES explanations(entity_id,id)
) STRICT;

CREATE TABLE allocations (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  explanation_id TEXT NOT NULL, line_id TEXT NOT NULL, posting_id TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(amount_micros<>0 AND amount_micros > -9223372036854775808),
  UNIQUE(entity_id,id), UNIQUE(explanation_id,posting_id),
  FOREIGN KEY(entity_id,explanation_id) REFERENCES explanations(entity_id,id),
  FOREIGN KEY(entity_id,line_id) REFERENCES statement_lines(entity_id,id),
  FOREIGN KEY(entity_id,posting_id) REFERENCES postings(entity_id,id)
) STRICT;

CREATE TABLE evidence_links (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  document_id TEXT NOT NULL, entry_id TEXT, statement_id TEXT,
  page INTEGER CHECK(page IS NULL OR page>0), locator TEXT NOT NULL, caption TEXT NOT NULL,
  CHECK((entry_id IS NOT NULL)+(statement_id IS NOT NULL)=1),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,document_id) REFERENCES documents(entity_id,id),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,statement_id) REFERENCES statements(entity_id,id)
) STRICT;

CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  kind TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  before TEXT CHECK(before IS NULL OR json_valid(before)), after TEXT CHECK(after IS NULL OR json_valid(after)),
  reason TEXT, request_id TEXT,
  UNIQUE(entity_id,seq),
  FOREIGN KEY(entity_id,request_id) REFERENCES requests(entity_id,id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE journal_mappings (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  source_revision TEXT NOT NULL, journal_path TEXT NOT NULL, source_index INTEGER NOT NULL,
  source_status TEXT NOT NULL, canonical_txn TEXT NOT NULL CHECK(json_valid(canonical_txn)),
  entry_id TEXT NOT NULL, txn_id TEXT,
  UNIQUE(entity_id,id), UNIQUE(entity_id,source_revision,journal_path,source_index),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id)
) STRICT;

CREATE TABLE close_records (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  through TEXT NOT NULL CHECK(date(through,'+0 days') IS through),
  prior_through TEXT CHECK(prior_through IS NULL OR date(prior_through,'+0 days') IS prior_through),
  snapshot_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('close','reopen')), reason TEXT NOT NULL,
  overrides TEXT NOT NULL CHECK(json_valid(overrides) AND json_type(overrides)='array'),
  -- a reopen overrides nothing: it is not gated on the checks, so there is never anything for it to waive
  CHECK(kind<>'reopen' OR overrides='[]'),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,snapshot_id) REFERENCES snapshots(entity_id,id)
) STRICT;

CREATE TABLE corrections (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  original_id TEXT NOT NULL UNIQUE, mirror_id TEXT NOT NULL UNIQUE, replacement_id TEXT,
  CHECK(original_id<>mirror_id AND (replacement_id IS NULL OR (replacement_id<>original_id AND replacement_id<>mirror_id))),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,original_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,mirror_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,replacement_id) REFERENCES entries(entity_id,id)
) STRICT;

CREATE TABLE document_expectations (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('awaiting','met','withdrawn')),
  awaiting TEXT NOT NULL CHECK(length(trim(awaiting))>0),
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  evidence_id TEXT, supersedes_id TEXT UNIQUE,
  -- a met expectation names the document that met it, and nothing else may
  CHECK((state='met') = (evidence_id IS NOT NULL)),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,evidence_id) REFERENCES evidence_links(entity_id,id),
  FOREIGN KEY(entity_id,supersedes_id) REFERENCES document_expectations(entity_id,id)
) STRICT;

CREATE TABLE unrecovered_tax (
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  entry_id TEXT NOT NULL, posting_id TEXT,
  commodity TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(amount_micros > 0),
  state TEXT NOT NULL CHECK(state IN ('unrecovered','recovered','forgone')),
  tax_code TEXT,
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  recoverable_until TEXT CHECK(recoverable_until IS NULL OR date(recoverable_until,'+0 days') IS recoverable_until),
  recovered_entry_id TEXT, supersedes_id TEXT UNIQUE,
  -- a recovered amount names the entry that recovered it, and nothing else may
  CHECK((state='recovered') = (recovered_entry_id IS NOT NULL)),
  UNIQUE(entity_id,id),
  FOREIGN KEY(entity_id,entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,posting_id) REFERENCES postings(entity_id,id),
  FOREIGN KEY(entity_id,commodity) REFERENCES commodities(entity_id,code),
  FOREIGN KEY(entity_id,recovered_entry_id) REFERENCES entries(entity_id,id),
  FOREIGN KEY(entity_id,supersedes_id) REFERENCES unrecovered_tax(entity_id,id)
) STRICT;
CREATE INDEX ix_postings_acct ON postings(account_id,entry_id,seq);

CREATE INDEX ix_ops_state ON operations(state,lease_until);

CREATE INDEX ix_entries_dt ON entries(dt,id);

CREATE INDEX ix_lines_statement ON statement_lines(statement_id,dt,id);

CREATE INDEX ix_lines_dt ON statement_lines(dt,id);

CREATE INDEX ix_explanations_line ON explanations(line_id);

CREATE INDEX ix_allocations_line ON allocations(line_id);

CREATE INDEX ix_allocations_posting ON allocations(posting_id);

CREATE INDEX ix_allocations_explanation ON allocations(explanation_id);
CREATE VIEW live_statements AS
 SELECT s.* FROM statements s WHERE NOT EXISTS(SELECT 1 FROM statements n WHERE n.replaces_id=s.id);

CREATE VIEW live_explanations AS
 SELECT x.* FROM explanations x
 WHERE NOT EXISTS(SELECT 1 FROM explanations n WHERE n.supersedes_id=x.id)
   AND EXISTS(SELECT 1 FROM statement_lines l JOIN live_statements s ON s.id=l.statement_id WHERE l.id=x.line_id);

CREATE VIEW live_allocations AS
 SELECT a.id,a.entity_id,a.explanation_id,a.line_id,a.posting_id,a.amount_micros,
 l.dt AS line_dt,l.amount_micros AS line_amount,
 s.account_id AS line_account,s.commodity AS line_commodity,
 p.account_id AS posting_account,p.commodity AS posting_commodity,p.amount_micros AS posting_amount,
 e.dt AS posting_dt,e.status AS entry_status,acc.is_cash,p.entity_id AS posting_entity
 FROM allocations a JOIN live_explanations x ON x.id=a.explanation_id
 JOIN statement_lines l ON l.id=a.line_id JOIN statements s ON s.id=l.statement_id
 JOIN postings p ON p.id=a.posting_id JOIN entries e ON e.id=p.entry_id JOIN accounts acc ON acc.id=p.account_id;

CREATE VIEW bad_allocations AS
 SELECT id FROM live_allocations WHERE amount_micros=0 OR line_amount=0 OR posting_amount=0
 OR (amount_micros>0)<>(line_amount>0) OR (amount_micros>0)<>(posting_amount>0)
 OR line_account<>posting_account OR line_commodity<>posting_commodity
 OR entity_id<>posting_entity OR is_cash<>1 OR entry_status<>'posted';

CREATE VIEW overallocated AS
 SELECT line_id AS id FROM live_allocations GROUP BY line_id HAVING SUM(abs(amount_micros))>MAX(abs(line_amount))
 UNION ALL
 SELECT posting_id FROM live_allocations GROUP BY posting_id HAVING SUM(abs(amount_micros))>MAX(abs(posting_amount));
