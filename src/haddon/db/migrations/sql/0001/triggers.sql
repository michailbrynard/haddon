-- Trigger suite, migration 0001.  Same provenance as schema.sql: the `sqlite_master` text of a database
-- built by running the old 0001..0010 chain to head, so the reasoning written into each trigger is the
-- reasoning that was there before the squash.
--
-- These are the ledger's real guarantees -- append-only tables refusing UPDATE and DELETE, amounts held to
-- their commodity's quantum, postings refusing to move once their period is locked.  The schema says what
-- the shape is; this file says what cannot happen to it.
--
-- This file is frozen: migration 0001 pins its sha256.

CREATE TRIGGER postings_quantum_insert BEFORE INSERT ON postings BEGIN SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM commodities c WHERE c.code=NEW.commodity AND c.entity_id=NEW.entity_id AND NEW.amount_micros%c.quantum=0); END;

CREATE TRIGGER postings_quantum_update BEFORE UPDATE ON postings BEGIN SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM commodities c WHERE c.code=NEW.commodity AND c.entity_id=NEW.entity_id AND NEW.amount_micros%c.quantum=0); END;

CREATE TRIGGER postings_insert BEFORE INSERT ON postings BEGIN
 SELECT RAISE(ABORT,'parent_or_lock') WHERE NOT EXISTS(SELECT 1 FROM entries e,period_locks l WHERE e.id=NEW.entry_id AND e.status='draft' AND (l.through IS NULL OR e.dt>l.through));
END;

CREATE TRIGGER postings_update BEFORE UPDATE ON postings BEGIN
 SELECT RAISE(ABORT,'immutable') WHERE NEW.id IS NOT OLD.id OR NEW.entity_id IS NOT OLD.entity_id;
 SELECT RAISE(ABORT,'old_parent_or_lock') WHERE NOT EXISTS(SELECT 1 FROM entries e,period_locks l WHERE e.id=OLD.entry_id AND e.status='draft' AND (l.through IS NULL OR e.dt>l.through));
 SELECT RAISE(ABORT,'new_parent_or_lock') WHERE NOT EXISTS(SELECT 1 FROM entries e,period_locks l WHERE e.id=NEW.entry_id AND e.status='draft' AND (l.through IS NULL OR e.dt>l.through));
END;

CREATE TRIGGER postings_delete BEFORE DELETE ON postings BEGIN
 SELECT RAISE(ABORT,'parent_or_lock') WHERE NOT EXISTS(SELECT 1 FROM entries e,period_locks l WHERE e.id=OLD.entry_id AND e.status='draft' AND (l.through IS NULL OR e.dt>l.through));
END;

CREATE TRIGGER locks_insert BEFORE INSERT ON period_locks BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM period_locks);
END;

CREATE TRIGGER locks_update BEFORE UPDATE ON period_locks BEGIN
 SELECT RAISE(ABORT,'lock_invalid') WHERE NEW.id IS NOT OLD.id OR NEW.entity_id IS NOT OLD.entity_id OR NEW.singleton IS NOT OLD.singleton OR NEW.version<>OLD.version+1;
 SELECT RAISE(ABORT,'close_record_required') WHERE NOT EXISTS(SELECT 1 FROM close_records c WHERE c.id=NEW.close_id AND c.prior_through IS OLD.through AND c.through IS NEW.through AND ((c.kind='close' AND (OLD.through IS NULL OR NEW.through>OLD.through)) OR (c.kind='reopen' AND length(trim(c.reason))>0 AND NEW.through<OLD.through)));
END;

CREATE TRIGGER operations_update BEFORE UPDATE ON operations BEGIN SELECT RAISE(ABORT,'operation_state') WHERE OLD.state IN ('succeeded','failed') OR NEW.id IS NOT OLD.id OR NEW.entity_id IS NOT OLD.entity_id OR NEW.intent IS NOT OLD.intent OR NEW.kind IS NOT OLD.kind OR NEW.version<>OLD.version+1; END;

CREATE TRIGGER operations_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'retain_operation'); END;

CREATE TRIGGER locks_delete BEFORE DELETE ON period_locks BEGIN SELECT RAISE(ABORT,'retain_lock'); END;

CREATE TRIGGER accounts_update BEFORE UPDATE ON accounts BEGIN
 SELECT RAISE(ABORT,'chart_identity') WHERE NEW.id IS NOT OLD.id OR NEW.entity_id IS NOT OLD.entity_id OR NEW.version<>OLD.version+1;
 SELECT RAISE(ABORT,'referenced_chart_immutable') WHERE EXISTS(SELECT 1 FROM postings WHERE account_id=OLD.id) AND (NEW.path IS NOT OLD.path OR NEW.type IS NOT OLD.type OR NEW.is_cash IS NOT OLD.is_cash OR NEW.code IS NOT OLD.code OR NEW.tax_code IS NOT OLD.tax_code);
END;

CREATE TRIGGER accounts_delete BEFORE DELETE ON accounts BEGIN SELECT RAISE(ABORT,'archive_account'); END;

CREATE TRIGGER entities_u BEFORE UPDATE ON entities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER entities_d BEFORE DELETE ON entities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER commodities_u BEFORE UPDATE ON commodities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER commodities_d BEFORE DELETE ON commodities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER allowed_u BEFORE UPDATE ON account_commodities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER allowed_d BEFORE DELETE ON account_commodities BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER requests_u BEFORE UPDATE ON requests BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER requests_d BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER exports_u BEFORE UPDATE ON exports BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER exports_d BEFORE DELETE ON exports BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER entries_insert BEFORE INSERT ON entries BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM entries WHERE id=NEW.id);  -- D1 defence in depth
 SELECT RAISE(ABORT,'draft_required') WHERE NEW.status<>'draft';
 SELECT RAISE(ABORT,'period_locked') WHERE NOT EXISTS(SELECT 1 FROM period_locks WHERE through IS NULL OR NEW.dt>through);
END;

CREATE TRIGGER entries_update BEFORE UPDATE ON entries BEGIN
 SELECT RAISE(ABORT,'immutable') WHERE OLD.status<>'draft' OR NEW.id IS NOT OLD.id OR NEW.entity_id IS NOT OLD.entity_id;
 SELECT RAISE(ABORT,'version_conflict') WHERE NEW.version<>OLD.version+1;
 -- D4: a draft is not accounting, so draft->void is allowed under a lock; the exemption applies only when
 --     nothing but status/version changes.  Every other mutation of a locked draft stays refused.
 -- E1: code/comment/tags join that enumeration, on exactly the same footing as narration.
 SELECT RAISE(ABORT,'period_locked') WHERE NOT (NEW.status='void' AND NEW.dt IS OLD.dt AND NEW.narration IS OLD.narration AND NEW.origin IS OLD.origin AND NEW.created_at IS OLD.created_at AND NEW.created_by IS OLD.created_by AND NEW.code IS OLD.code AND NEW.comment IS OLD.comment AND NEW.tags IS OLD.tags)
  AND NOT EXISTS(SELECT 1 FROM period_locks WHERE through IS NULL OR (OLD.dt>through AND NEW.dt>through));
 SELECT RAISE(ABORT,'two_legs_required') WHERE NEW.status='posted' AND (SELECT count(*) FROM postings WHERE entry_id=OLD.id)<2;
 SELECT RAISE(ABORT,'unbalanced') WHERE NEW.status='posted' AND EXISTS(SELECT 1 FROM postings WHERE entry_id=OLD.id GROUP BY commodity HAVING SUM(amount_micros)<>0);
 SELECT RAISE(ABORT,'account_invalid') WHERE NEW.status='posted' AND EXISTS(
  SELECT 1 FROM postings p LEFT JOIN accounts a ON a.id=p.account_id WHERE p.entry_id=OLD.id AND (a.id IS NULL OR a.archived<>0 OR a.entity_id<>NEW.entity_id));
END;

CREATE TRIGGER entries_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'retain_entry'); END;

CREATE TRIGGER snapshots_u BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER snapshots_d BEFORE DELETE ON snapshots BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER events_insert BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'exists') WHERE NEW.seq IS NOT NULL AND EXISTS(SELECT 1 FROM events WHERE seq=NEW.seq); END;

CREATE TRIGGER events_u BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER events_d BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER mappings_u BEFORE UPDATE ON journal_mappings BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER mappings_d BEFORE DELETE ON journal_mappings BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER documents_u BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER documents_d BEFORE DELETE ON documents BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER statements_u BEFORE UPDATE ON statements BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER statements_d BEFORE DELETE ON statements BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER statement_lines_u BEFORE UPDATE ON statement_lines BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER statement_lines_d BEFORE DELETE ON statement_lines BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER explanations_u BEFORE UPDATE ON explanations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER explanations_d BEFORE DELETE ON explanations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER allocations_u BEFORE UPDATE ON allocations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER allocations_d BEFORE DELETE ON allocations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER evidence_u BEFORE UPDATE ON evidence_links BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER evidence_d BEFORE DELETE ON evidence_links BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER statements_quantum_insert BEFORE INSERT ON statements BEGIN
 SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM commodities c WHERE c.code=NEW.commodity AND c.entity_id=NEW.entity_id AND NEW.opening_micros%c.quantum=0 AND NEW.closing_micros%c.quantum=0);
END;

CREATE TRIGGER statement_lines_quantum_insert BEFORE INSERT ON statement_lines BEGIN
 SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM statements s JOIN commodities c ON c.code=s.commodity AND c.entity_id=s.entity_id WHERE s.id=NEW.statement_id AND NEW.amount_micros%c.quantum=0);
END;

CREATE TRIGGER allocations_quantum_insert BEFORE INSERT ON allocations BEGIN
 SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM statement_lines l JOIN statements s ON s.id=l.statement_id JOIN commodities c ON c.code=s.commodity AND c.entity_id=s.entity_id WHERE l.id=NEW.line_id AND NEW.amount_micros%c.quantum=0);
END;

CREATE TRIGGER statements_insert BEFORE INSERT ON statements BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM statements WHERE id=NEW.id);
 SELECT RAISE(ABORT,'period_locked') WHERE NOT EXISTS(SELECT 1 FROM period_locks WHERE through IS NULL OR NEW.period_start>through);
 SELECT RAISE(ABORT,'old_period_locked') WHERE NEW.replaces_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM statements s,period_locks l WHERE s.id=NEW.replaces_id AND (l.through IS NULL OR s.period_start>l.through));
 SELECT RAISE(ABORT,'replaces_subject') WHERE NEW.replaces_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM statements s WHERE s.id=NEW.replaces_id AND s.account_id=NEW.account_id AND s.commodity=NEW.commodity);
 -- A booked line rests on the input being replaced.  Undo the booking first; a replacement must not quietly
 -- orphan an entry it caused.
 SELECT RAISE(ABORT,'lines_still_booked') WHERE NEW.replaces_id IS NOT NULL AND EXISTS(SELECT 1 FROM live_explanations x JOIN statement_lines l ON l.id=x.line_id WHERE l.statement_id=NEW.replaces_id AND x.kind='booked');
 SELECT RAISE(ABORT,'period_overlap') WHERE EXISTS(SELECT 1 FROM live_statements s WHERE s.entity_id=NEW.entity_id AND s.account_id=NEW.account_id AND s.commodity=NEW.commodity AND s.period_start<=NEW.period_end AND s.period_end>=NEW.period_start AND s.id IS NOT NEW.replaces_id);
 -- One document is one LIVE statement.  `UNIQUE(document_id)` said "one statement, ever", which made the
 -- ordinary correction -- the parse was wrong, here is the same download read properly -- impossible without
 -- different bytes.  The statement being replaced is excluded because at this point it is still live.
 SELECT RAISE(ABORT,'document_already_loaded') WHERE EXISTS(SELECT 1 FROM live_statements s WHERE s.entity_id=NEW.entity_id AND s.document_id=NEW.document_id AND s.id IS NOT NEW.replaces_id);
 -- Replacing a statement retires its lines' explanations, so their allocations stop consuming their postings.
 -- Releasing a posting in a closed month is a change to a closed month; refuse it as one.
 SELECT RAISE(ABORT,'old_period_locked') WHERE NEW.replaces_id IS NOT NULL AND EXISTS(SELECT 1 FROM live_allocations a JOIN statement_lines l ON l.id=a.line_id, period_locks k WHERE l.statement_id=NEW.replaces_id AND k.through IS NOT NULL AND a.posting_dt<=k.through);
END;

CREATE TRIGGER statement_lines_insert BEFORE INSERT ON statement_lines BEGIN
 SELECT RAISE(ABORT,'period_locked') WHERE NOT EXISTS(SELECT 1 FROM period_locks WHERE through IS NULL OR NEW.dt>through);
 SELECT RAISE(ABORT,'outside_period') WHERE NOT EXISTS(SELECT 1 FROM statements s WHERE s.id=NEW.statement_id AND s.entity_id=NEW.entity_id AND NEW.dt BETWEEN s.period_start AND s.period_end);
END;

CREATE TRIGGER explanations_insert BEFORE INSERT ON explanations BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM explanations WHERE id=NEW.id);
 SELECT RAISE(ABORT,'period_locked') WHERE NOT EXISTS(SELECT 1 FROM statement_lines l,period_locks k WHERE l.id=NEW.line_id AND (k.through IS NULL OR l.dt>k.through));
 SELECT RAISE(ABORT,'statement_replaced') WHERE NOT EXISTS(SELECT 1 FROM statement_lines l JOIN live_statements s ON s.id=l.statement_id WHERE l.id=NEW.line_id);
 SELECT RAISE(ABORT,'supersedes_subject') WHERE NEW.supersedes_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM explanations x WHERE x.id=NEW.supersedes_id AND x.line_id=NEW.line_id);
 -- one live explanation per line: a second one must say which it replaces, and that one must still be live.
 SELECT RAISE(ABORT,'already_explained') WHERE EXISTS(SELECT 1 FROM live_explanations x WHERE x.line_id=NEW.line_id AND x.id IS NOT NEW.supersedes_id);
 SELECT RAISE(ABORT,'not_live') WHERE NEW.supersedes_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM live_explanations x WHERE x.id=NEW.supersedes_id);
 -- Inserting an allocation checks the line's date AND the posting's entry date; RETIRING one was checked on
 -- neither.  A `linked` explanation has no `entry_id`, so nothing else here looks at where its postings are
 -- dated -- and superseding it would quietly unexplain a closed month's postings.
 SELECT RAISE(ABORT,'period_locked') WHERE NEW.supersedes_id IS NOT NULL AND EXISTS(SELECT 1 FROM live_allocations a,period_locks k WHERE a.explanation_id=NEW.supersedes_id AND k.through IS NOT NULL AND a.posting_dt<=k.through);
END;

CREATE TRIGGER allocations_insert BEFORE INSERT ON allocations BEGIN
 SELECT RAISE(ABORT,'explanation_not_live') WHERE NOT EXISTS(SELECT 1 FROM live_explanations x WHERE x.id=NEW.explanation_id AND x.line_id=NEW.line_id AND x.kind<>'ignored');
 SELECT RAISE(ABORT,'period_locked') WHERE NOT EXISTS(SELECT 1 FROM statement_lines l,postings p,entries e,period_locks k WHERE l.id=NEW.line_id AND p.id=NEW.posting_id AND e.id=p.entry_id AND (k.through IS NULL OR (l.dt>k.through AND e.dt>k.through)));
END;

CREATE TRIGGER allocations_validate_insert AFTER INSERT ON allocations BEGIN
 SELECT RAISE(ABORT,'allocation_invalid') WHERE EXISTS(SELECT 1 FROM bad_allocations WHERE id=NEW.id);
 SELECT RAISE(ABORT,'overallocated') WHERE EXISTS(SELECT 1 FROM overallocated WHERE id IN (NEW.line_id,NEW.posting_id));
END;

CREATE TRIGGER evidence_insert BEFORE INSERT ON evidence_links BEGIN
 SELECT RAISE(ABORT,'period_locked') WHERE EXISTS(SELECT 1 FROM period_locks k WHERE k.through IS NOT NULL AND EXISTS(SELECT 1 FROM statements s WHERE s.id=NEW.statement_id AND s.period_start<=k.through));
END;

CREATE TRIGGER closes_u BEFORE UPDATE ON close_records BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER closes_d BEFORE DELETE ON close_records BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER corrections_insert BEFORE INSERT ON corrections BEGIN
 SELECT RAISE(ABORT,'correction_invalid') WHERE NOT EXISTS(SELECT 1 FROM entries o,entries m,period_locks l WHERE o.id=NEW.original_id AND m.id=NEW.mirror_id AND o.status='posted' AND m.status='posted' AND m.origin='reversal' AND (l.through IS NULL OR m.dt>l.through));
 SELECT RAISE(ABORT,'replacement_invalid') WHERE NEW.replacement_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM entries e,period_locks l WHERE e.id=NEW.replacement_id AND e.status='posted' AND (l.through IS NULL OR e.dt>l.through));
END;

CREATE TRIGGER corrections_u BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER corrections_d BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER expectations_insert BEFORE INSERT ON document_expectations BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM document_expectations WHERE id=NEW.id);
 -- a resolution resolves something: 'met' and 'withdrawn' always supersede a live 'awaiting' row on the SAME entry
 SELECT RAISE(ABORT,'nothing_awaited') WHERE NEW.state<>'awaiting' AND NEW.supersedes_id IS NULL;
 SELECT RAISE(ABORT,'supersedes_subject') WHERE NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM document_expectations p WHERE p.id=NEW.supersedes_id AND p.entry_id=NEW.entry_id AND p.state='awaiting');
 -- one live expectation per entry, so "what is provisional?" is a query and not a fold over a history
 SELECT RAISE(ABORT,'already_awaiting') WHERE NEW.state='awaiting' AND NEW.supersedes_id IS NULL AND EXISTS(
  SELECT 1 FROM document_expectations o WHERE o.entry_id=NEW.entry_id AND o.state='awaiting'
   AND NOT EXISTS(SELECT 1 FROM document_expectations n WHERE n.supersedes_id=o.id));
END;

CREATE TRIGGER expectations_u BEFORE UPDATE ON document_expectations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER expectations_d BEFORE DELETE ON document_expectations BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER unrecovered_quantum_insert BEFORE INSERT ON unrecovered_tax BEGIN SELECT RAISE(ABORT,'precision') WHERE NOT EXISTS(SELECT 1 FROM commodities c WHERE c.code=NEW.commodity AND c.entity_id=NEW.entity_id AND NEW.amount_micros%c.quantum=0); END;

CREATE TRIGGER unrecovered_insert BEFORE INSERT ON unrecovered_tax BEGIN
 SELECT RAISE(ABORT,'exists') WHERE EXISTS(SELECT 1 FROM unrecovered_tax WHERE id=NEW.id);
 -- the leg named must be a leg of the entry named: "where is this amount hiding" has to be true or absent
 SELECT RAISE(ABORT,'posting_not_of_entry') WHERE NEW.posting_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM postings p WHERE p.id=NEW.posting_id AND p.entry_id=NEW.entry_id AND p.entity_id=NEW.entity_id);
 -- a resolution resolves something: 'recovered' and 'forgone' supersede a live 'unrecovered' row of the same subject
 SELECT RAISE(ABORT,'nothing_unrecovered') WHERE NEW.state<>'unrecovered' AND NEW.supersedes_id IS NULL;
 SELECT RAISE(ABORT,'supersedes_subject') WHERE NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM unrecovered_tax p WHERE p.id=NEW.supersedes_id AND p.entry_id=NEW.entry_id
   AND p.commodity=NEW.commodity AND p.state='unrecovered');
 -- one live 'unrecovered' record per entry and commodity, so the total cannot count one amount twice
 SELECT RAISE(ABORT,'already_recorded') WHERE NEW.state='unrecovered' AND NEW.supersedes_id IS NULL AND EXISTS(
  SELECT 1 FROM unrecovered_tax o WHERE o.entry_id=NEW.entry_id AND o.commodity=NEW.commodity
   AND o.state='unrecovered' AND NOT EXISTS(SELECT 1 FROM unrecovered_tax n WHERE n.supersedes_id=o.id));
 -- the entry that recovered it is not the entry it was unrecovered on: a recovery is its own economic event
 SELECT RAISE(ABORT,'recovery_is_the_same_entry') WHERE NEW.recovered_entry_id IS NOT NULL AND NEW.recovered_entry_id=NEW.entry_id;
END;

CREATE TRIGGER unrecovered_u BEFORE UPDATE ON unrecovered_tax BEGIN SELECT RAISE(ABORT,'immutable'); END;

CREATE TRIGGER unrecovered_d BEFORE DELETE ON unrecovered_tax BEGIN SELECT RAISE(ABORT,'immutable'); END;
