"""Forbidden-mutation matrix: the spec's mandatory deliverable and the acceptance gate for the trigger suite.

Every `refused(...)` case attempts a mutation the spec (or the review's fixes) says the database must refuse;
the test passes only if SQLite raises.  Every `allowed(...)` case is a positive control: a mutation the schema
must accept, so the suite cannot go green by refusing everything.  Each case runs on a fresh guarded copy of
the migrated+seeded template (see conftest.py); setup failures raise FixtureSetupError and are reported as
SETUP-ERROR, never as a pass.

Sections: entries / sealing / postings / period lock on every guarded table / period_locks & close_records /
allocations / corrections / immutable & append-only tables / conflict-resolution REPLACE (review D1) /
review fixes D1-D5 (D3's subject was dropped in G1) / under-specification resolutions U3-U7 (docs/design/decisions.md).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

import pytest

from haddon.db.connection import sqlite_connect

from conftest import FixtureSetupError, restore_into_memory
from seed import ins

BIG = 9223372036854775807
C = "entity_id,created_at,created_by"
CV = "'E','t','who'"

Stmt = str | tuple[str, tuple]


@dataclass
class Case:
    name: str
    sql: str
    params: tuple = ()
    locked: bool = False
    setup: list[Stmt] = field(default_factory=list)
    expect_refused: bool = True
    unguarded_pragmas: dict[str, str] | None = None  # test-only: bypass the connection guard (D1 defence in depth)
    note: str | None = None
    expect: str | None = None  # substring the refusal message must contain (which guard fired)


CASES: list[Case] = []


def refused(name, sql, params=(), locked=False, setup=None, note=None, unguarded_pragmas=None, expect=None):
    CASES.append(Case(name, sql, params, locked, list(setup or []), True, unguarded_pragmas, note, expect))


def allowed(name, sql, params=(), locked=False, setup=None, note=None):
    CASES.append(Case(name, sql, params, locked, list(setup or []), False, None, note))


# entries
refused('entries: direct INSERT status=posted', f"INSERT INTO entries(id,{C},dt,narration,status,origin) VALUES('x',{CV},'2026-03-01','n','posted','manual')")
refused('entries: direct INSERT status=void', f"INSERT INTO entries(id,{C},dt,narration,status,origin) VALUES('x',{CV},'2026-03-01','n','void','manual')")
refused('entries: UPDATE posted narration (fail-open probe)', "UPDATE entries SET narration='hacked', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE posted narration, no version bump', "UPDATE entries SET narration='hacked' WHERE id='e_posted'")
refused('entries: UPDATE posted dt', "UPDATE entries SET dt='2026-03-20', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE posted origin', "UPDATE entries SET origin='manual', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE posted -> draft', "UPDATE entries SET status='draft', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE posted -> void', "UPDATE entries SET status='void', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE posted created_by', "UPDATE entries SET created_by='x', version=version+1 WHERE id='e_posted'")
refused('entries: UPDATE void narration', "UPDATE entries SET narration='h', version=version+1 WHERE id='e_void'")
refused('entries: UPDATE void -> draft', "UPDATE entries SET status='draft', version=version+1 WHERE id='e_void'")
refused('entries: UPDATE void -> posted', "UPDATE entries SET status='posted', version=version+1 WHERE id='e_void'")
refused('entries: UPDATE draft id', "UPDATE entries SET id='e_new', version=version+1 WHERE id='e_draft'")
refused('entries: UPDATE draft entity_id', "UPDATE entries SET entity_id='F', version=version+1 WHERE id='e_draft'")
refused('entries: UPDATE draft without version bump', "UPDATE entries SET narration='n2' WHERE id='e_draft'")
refused('entries: UPDATE draft version +2', "UPDATE entries SET narration='n2', version=version+2 WHERE id='e_draft'")
refused('entries: INSERT invalid calendar date', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-02-30','n','manual')")
refused('entries: INSERT bad date shape', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-3-1','n','manual')")
refused('entries: INSERT unknown origin', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-03-01','n','bogus')")
# E1 (migration 0002): the transaction note fields are covered by the same immutability rule as the narration.
refused('entries: UPDATE posted code', "UPDATE entries SET code='HACKED', version=version+1 WHERE id='e_posted'", expect='immutable')
refused('entries: UPDATE posted comment', "UPDATE entries SET comment='hacked', version=version+1 WHERE id='e_posted'", expect='immutable')
refused('entries: UPDATE posted tags', "UPDATE entries SET tags='[[\"t\",\"1\"]]', version=version+1 WHERE id='e_posted'", expect='immutable')
refused('entries: UPDATE void comment', "UPDATE entries SET comment='hacked', version=version+1 WHERE id='e_void'", expect='immutable')
refused('entries: INSERT tags that are not JSON', f"INSERT INTO entries(id,{C},dt,narration,origin,tags) VALUES('x',{CV},'2026-03-01','n','manual','nope')", expect='json_valid')
allowed('entries: INSERT draft with code/comment/tags (positive control)', f"INSERT INTO entries(id,{C},dt,narration,origin,code,comment,tags) VALUES('x',{CV},'2026-03-01','n','manual','C','c',\'[[\"t\",\"1\"]]\')")
allowed('entries: UPDATE draft comment (a draft is not accounting: positive control)', "UPDATE entries SET comment='note', version=version+1 WHERE id='e_draft'")
refused('entries: DELETE posted', "DELETE FROM entries WHERE id='e_posted'")
refused('entries: DELETE draft', "DELETE FROM entries WHERE id='e_draft'")
refused('entries: DELETE void', "DELETE FROM entries WHERE id='e_void'")
refused('entries: DELETE all', "DELETE FROM entries")
# sealing
refused('seal: zero legs', "UPDATE entries SET status='posted', version=2 WHERE id='e0'",
        setup=[f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('e0',{CV},'2026-03-01','n','manual')"])
refused('seal: one leg', "UPDATE entries SET status='posted', version=2 WHERE id='e1'",
        setup=[f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('e1',{CV},'2026-03-01','n','manual')",
               f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('e1.p1',{CV},'e1',1,'cash','USD',-10000)"])
refused('seal: unbalanced', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=["UPDATE postings SET amount_micros=6000000 WHERE id='e_draft.p2'"])
refused('seal: unbalanced two legs same sign', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=["UPDATE postings SET amount_micros=5000000 WHERE id='e_draft.p1'"])
refused('seal: multi-commodity, each unbalanced', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=["UPDATE postings SET commodity='EUR' WHERE id='e_draft.p2'"])
allowed('seal: multi-commodity balanced per commodity (U5: allowed)', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=[f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x3',{CV},'e_draft',3,'cash-eur','EUR',-10000)",
               f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x4',{CV},'e_draft',4,'exp','EUR',10000)"])
refused('seal: archived account leg', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=["UPDATE postings SET account_id='arch' WHERE id='e_draft.p2'"])
refused('seal: account archived after leg inserted', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=["UPDATE accounts SET archived=1, version=2 WHERE id='exp'"])
refused('seal: version not bumped', "UPDATE entries SET status='posted' WHERE id='e_draft'")
refused('seal: draft in locked period (pre-existing draft)', "UPDATE entries SET status='posted', version=2 WHERE id='e_jan_draft'", locked=True)
refused('seal: overflow on SUM of legs', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'",
        setup=[f"UPDATE postings SET amount_micros={BIG-BIG%10000} WHERE id='e_draft.p1'",
               f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x3',{CV},'e_draft',3,'cash','USD',{BIG-BIG%10000})"])
allowed('seal: balanced two-leg draft (positive control)', "UPDATE entries SET status='posted', version=2 WHERE id='e_draft'")
allowed('seal: draft -> void (positive control)', "UPDATE entries SET status='void', version=2 WHERE id='e_draft'")

# postings
refused('postings: INSERT into posted entry', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_posted',3,'cash','USD',10000)")
refused('postings: INSERT into void entry', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_void',3,'cash','USD',10000)")
refused('postings: INSERT orphan entry_id', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'nope',1,'cash','USD',10000)")
refused('postings: UPDATE posted amount', "UPDATE postings SET amount_micros=-13000000 WHERE id='e_posted.p1'")
refused('postings: UPDATE posted account', "UPDATE postings SET account_id='noncash' WHERE id='e_posted.p2'")
refused('postings: UPDATE posted memo', "UPDATE postings SET memo='m' WHERE id='e_posted.p1'")
refused('postings: UPDATE posted tax_code', "UPDATE postings SET tax_code='t' WHERE id='e_posted.p1'")
refused('postings: UPDATE void amount', "UPDATE postings SET amount_micros=-13000000 WHERE id='e_void.p1'")
refused('postings: UPDATE draft id', "UPDATE postings SET id='z' WHERE id='e_draft.p1'")
refused('postings: UPDATE draft entity_id', "UPDATE postings SET entity_id='F' WHERE id='e_draft.p1'")
refused('postings: DELETE from posted', "DELETE FROM postings WHERE id='e_posted.p1'")
refused('postings: DELETE from void', "DELETE FROM postings WHERE id='e_void.p1'")
refused('postings: reparent draft leg -> posted entry (NEW)', "UPDATE postings SET entry_id='e_posted', seq=9 WHERE id='e_draft.p1'")
refused('postings: reparent posted leg -> draft entry (OLD)', "UPDATE postings SET entry_id='e_draft', seq=9 WHERE id='e_posted.p1'")
refused('postings: reparent draft leg -> void entry', "UPDATE postings SET entry_id='e_void', seq=9 WHERE id='e_draft.p1'")
refused('postings: reparent draft leg -> locked draft', "UPDATE postings SET entry_id='e_jan_draft', seq=9 WHERE id='e_draft.p1'", locked=True)
refused('postings: INSERT undeclared account', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'nope','USD',10000)")
refused('postings: INSERT wrong commodity for account', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','EUR',10000)")
refused('postings: INSERT unknown commodity', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','GBP',10000)")
refused('postings: INSERT cross-entity (entity_id != entry entity)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x','F','t','w','e_draft',3,'cash','USD',10000)")
refused('postings: INSERT quantum violation (12345 @ 2dp)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD',12345)")
refused('postings: UPDATE quantum violation', "UPDATE postings SET amount_micros=12345 WHERE id='e_draft.p1'")
refused('postings: INSERT -2^63', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD',-9223372036854775808)")
refused('postings: INSERT 2^63 (overflow)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD',9223372036854775808)")
refused('postings: INSERT REAL amount (STRICT)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD',1.5)")
refused('postings: INSERT text amount (STRICT)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD','12abc')")
refused('postings: INSERT seq=0', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',0,'cash','USD',10000)")
refused('postings: INSERT duplicate seq', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',1,'cash','USD',10000)")
allowed('postings: INSERT archived account into draft (U8: caught only at seal)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'arch','USD',10000)")
allowed('postings: INSERT valid leg into open draft (positive control)', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_draft',3,'cash','USD',10000)")

# locked period, every guarded table
L = dict(locked=True)
refused('lock: entries INSERT dated in locked period', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-02-10','n','manual')", **L)
refused('lock: entries INSERT dated on lock boundary', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-02-28','n','manual')", **L)
refused('lock: entries UPDATE draft dated in locked period', "UPDATE entries SET narration='n', version=2 WHERE id='e_jan_draft'", **L)
refused('lock: entries UPDATE draft dt INTO locked period', "UPDATE entries SET dt='2026-02-01', version=2 WHERE id='e_draft'", **L)
refused('lock: entries UPDATE locked draft dt OUT of locked period', "UPDATE entries SET dt='2026-03-01', version=2 WHERE id='e_jan_draft'", **L)
refused('lock: postings INSERT into locked draft', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('x',{CV},'e_jan_draft',3,'cash','USD',10000)", **L)
refused('lock: postings UPDATE in locked draft', "UPDATE postings SET amount_micros=-2000000 WHERE id='e_jan_draft.p1'", **L)
refused('lock: postings DELETE in locked draft', "DELETE FROM postings WHERE id='e_jan_draft.p1'", **L)
refused('lock: allocations INSERT on a locked line', f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},'x_jan','r_jan','e_rev_jan.p1',7000000)", **L)
refused('lock: allocations INSERT locked posting, open line', f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},'x_r4','r4','e_jan.p1',-5000000)", **L,
        setup=[f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES('x_r4',{CV},'r4','linked','r')"])
refused('lock: explanations INSERT on a locked line', f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES('x',{CV},'r_jan','ignored','r')", **L)
refused('lock: explanations supersede an explanation of a locked line', f"INSERT INTO explanations(id,{C},line_id,kind,reason,supersedes_id) VALUES('x',{CV},'r_jan','ignored','r','x_jan')", **L)
refused('lock: statement_lines INSERT dated in locked period', f"INSERT INTO statement_lines(id,{C},statement_id,line_no,source_key,content_hash,dt,amount_micros,description,fields,raw) VALUES('x',{CV},'s_jan',99,'k99','h99','2026-01-25',-10000,'d','{{}}','r')", **L)
refused('lock: statements INSERT starting in locked period', f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason) VALUES('x',{CV},'f4','cash','USD','2026-02-01','2026-02-28',0,0,'l','r')", **L)
refused('lock: statements replace a statement starting in locked period', f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason,replaces_id) VALUES('x',{CV},'f4','cash','USD','2026-01-01','2026-01-31',0,0,'l','r','s_jan')", **L)
refused('lock: corrections mirror dated in locked period', f"INSERT INTO corrections(id,{C},original_id,mirror_id) VALUES('x',{CV},'e_jan','e_rev_jan')", **L)
refused('lock: corrections replacement dated in locked period', f"INSERT INTO corrections(id,{C},original_id,mirror_id,replacement_id) VALUES('x',{CV},'e_posted','e_rev','e_jan')", **L)
refused('lock: missing period_locks row => everything refused', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-03-01','n','manual')",
        setup=["DROP TRIGGER locks_delete", "DELETE FROM period_locks"])
allowed('lock: entries INSERT dated after lock (positive control)', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('x',{CV},'2026-03-01','n','manual')", **L)

# period_locks / close_records
refused('locks: advance without close_records row', "UPDATE period_locks SET through='2026-03-31', version=2 WHERE id='lock'")
refused('locks: advance with close_id of wrong prior_through', "UPDATE period_locks SET through='2026-03-31', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-01-31','sn2','close','x','[]')"])
refused('locks: advance with close_id whose through differs', "UPDATE period_locks SET through='2026-04-30', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','close','x','[]')"])
refused('locks: advance without version bump', "UPDATE period_locks SET through='2026-03-31', close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','close','x','[]')"])
refused("locks: 'close' record that moves through backwards", "UPDATE period_locks SET through='2026-01-31', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-01-31','2026-02-28','sn1','close','x','[]')"])
refused('locks: reopen with blank reason', "UPDATE period_locks SET through='2026-01-31', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-01-31','2026-02-28','sn1','reopen','   ','[]')"])
refused("locks: 'reopen' record that moves through forwards", "UPDATE period_locks SET through='2026-03-31', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','reopen','why','[]')"])
refused('locks: reuse an old close record', "UPDATE period_locks SET through='2026-02-28', version=3, close_id='cr1' WHERE id='lock'", locked=True)
refused('locks: set through=NULL directly', "UPDATE period_locks SET through=NULL, version=3, close_id=NULL WHERE id='lock'", locked=True)
refused('locks: change id/singleton', "UPDATE period_locks SET singleton=1, id='lock2', version=2 WHERE id='lock'")
refused('locks: DELETE', "DELETE FROM period_locks")
refused('locks: INSERT second lock row', f"INSERT INTO period_locks(id,{C},singleton) VALUES('lock2',{CV},1)")
refused('locks: INSERT lock with singleton=2', f"INSERT INTO period_locks(id,{C},singleton) VALUES('lock2',{CV},2)")
refused('close_records: UPDATE', "UPDATE close_records SET reason='x' WHERE id='cr1'", locked=True)
refused('close_records: DELETE', "DELETE FROM close_records WHERE id='cr1'", locked=True)
allowed('locks: reopen with reason (positive control)', "UPDATE period_locks SET through='2026-01-31', version=3, close_id='cr2' WHERE id='lock'", locked=True,
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-01-31','2026-02-28','sn1','reopen','why','[]')"])
allowed('locks: first close from no lock (positive control)', "UPDATE period_locks SET through='2026-02-28', version=2, close_id='crx' WHERE id='lock'",
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('crx',{CV},'2026-02-28',NULL,'sn1','close','x','[]')"])
# Issue 0005: an override lives on the close it let through.  The database enforces the two rules Python must
# not be the only keeper of: it is a JSON array, and a reopen (which is not gated on the checks) waives nothing.
refused('close_records: overrides is not JSON', f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','close','x','not json')", locked=True)
refused('close_records: overrides is a JSON object, not an array', f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','close','x','{{\"gate\":\"x\"}}')", locked=True)
refused('close_records: a reopen that waives a gate', f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-01-31','2026-02-28','sn1','reopen','why','[{{\"gate\":\"statement_coverage\"}}]')", locked=True)
allowed('close_records: a close that waives a gate (positive control)', f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},'2026-03-31','2026-02-28','sn2','close','x','[{{\"gate\":\"statement_coverage\",\"reason\":\"no statement yet\"}}]')", locked=True)
refused('locks: reopen to no lock (U7: close_records.through NOT NULL)', f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr2',{CV},NULL,'2026-02-28','sn1','reopen','why','[]')", locked=True, expect='NOT NULL')

# statements, lines, explanations and allocations (issue 0004)
D = f"INSERT INTO documents(id,{C},sha256,filename,media_type,size_bytes,blob_path) VALUES('x',{CV},"
ST = f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason"
SL = f"INSERT INTO statement_lines(id,{C},statement_id,line_no,source_key,content_hash,dt,amount_micros,description,fields,raw) VALUES('x',{CV},"
X = f"INSERT INTO explanations(id,{C},line_id,kind,reason"
A = f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},"
#: an explanation on the seed's one deliberately unexplained line, for the allocation probes to hang off
X4 = f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES('x_r4',{CV},'r4','linked','r')"

refused('doc: the same bytes twice', D + "'s1','a.csv','text/csv',1,'/z')")
refused('doc: neither stored bytes nor an external reference', f"INSERT INTO documents(id,{C},sha256,filename,media_type,size_bytes,blob_path) VALUES('x',{CV},'s9','a.csv','text/csv',1,'')")
refused('doc: both stored bytes and an external reference', f"INSERT INTO documents(id,{C},sha256,filename,media_type,size_bytes,blob_path,external_ref) VALUES('x',{CV},'s9','a.csv','text/csv',1,'/z','dms://9')")
allowed('doc: an external reference and no bytes (positive control)', f"INSERT INTO documents(id,{C},sha256,filename,media_type,size_bytes,blob_path,external_ref) VALUES('x',{CV},'s9','a.csv','text/csv',1,'','dms://9')")

refused('stmt: overlapping live statement, same account and commodity', ST + f") VALUES('x',{CV},'f4','cash','USD','2026-03-15','2026-04-15',0,0,'l','r')")
refused('stmt: period_end before period_start', ST + f") VALUES('x',{CV},'f4','cash','USD','2026-04-30','2026-04-01',0,0,'l','r')")
refused('stmt: closing off the commodity quantum', ST + f") VALUES('x',{CV},'f4','cash','USD','2026-04-01','2026-04-30',0,12345,'l','r')")
refused('stmt: blank reason', ST + f") VALUES('x',{CV},'f4','cash','USD','2026-04-01','2026-04-30',0,0,'l','   ')")
refused('stmt: replace a statement of a different account', ST + f",replaces_id) VALUES('x',{CV},'f4','cash-eur','EUR','2026-03-01','2026-03-31',0,0,'l','r','s1')")
# One document is one live statement (issue 0004).  `f2` is the document `s1` was read off, so loading it again
# is refused.  The same document resubmitted as `s1`'s replacement is the ordinary correction of a bad parse;
# a `UNIQUE(document_id)` key would make that third case impossible.
refused('stmt: a document a live statement already holds', ST + f") VALUES('x',{CV},'f2','cash','USD','2026-05-01','2026-05-31',0,0,'l','r')", expect='document_already_loaded')
refused('stmt: a document a live statement of another account holds', ST + f") VALUES('x',{CV},'f2','cash-eur','EUR','2026-05-01','2026-05-31',0,0,'l','r')", expect='document_already_loaded')
allowed('stmt: the same document re-read as the replacement (positive control)', ST + f",replaces_id) VALUES('x',{CV},'f2','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')")
allowed('stmt: a document whose only statement has been replaced (positive control)', ST + f") VALUES('y',{CV},'f2','cash','USD','2026-05-01','2026-05-31',0,0,'l','r')",
        setup=[ST + f",replaces_id) VALUES('x',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')"])
refused('stmt: second replacement of one statement', ST + f",replaces_id) VALUES('y',{CV},'f5','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')",
        setup=[ST + f",replaces_id) VALUES('x',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')"])
refused('stmt: replace a statement whose line is still booked', ST + f",replaces_id) VALUES('y',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')",
        setup=[f"INSERT INTO explanations(id,{C},line_id,kind,reason,entry_id) VALUES('x_book',{CV},'r4','booked','r','e_open')"],
        expect='lines_still_booked')
allowed('stmt: replace a statement whose lines are only linked or ignored (positive control)', ST + f",replaces_id) VALUES('x',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')")
allowed('stmt: a period the replaced one no longer occupies (positive control)', ST + f") VALUES('x',{CV},'f4','cash','USD','2026-04-01','2026-04-30',0,0,'l','r')")
refused('stmt: a second statement over one document', ST + f") VALUES('x',{CV},'f1','cash','USD','2026-04-01','2026-04-30',0,0,'l','r')")

refused('line: dated outside its statement period', SL + "'s1',99,'k99','h99','2026-04-01',-10000,'d','{}','r')")
refused('line: duplicate line_no within one statement', SL + "'s1',1,'k99','h99','2026-03-05',-10000,'d','{}','r')")
refused('line: duplicate source_key within one statement', SL + "'s1',99,'k1','h99','2026-03-05',-10000,'d','{}','r')")
refused('line: quantum violation', SL + "'s1',99,'k99','h99','2026-03-05',-12345,'d','{}','r')")
refused('line: zero amount', SL + "'s1',99,'k99','h99','2026-03-05',0,'d','{}','r')")
allowed('line: the same source_key under a DIFFERENT statement (a replacement restates it)', SL + "'s1e',99,'k2','h99','2026-03-05',-10000,'d','{}','r')")

refused('explain: a second live explanation on one line', X + f") VALUES('x',{CV},'r1','ignored','r')")
refused('explain: supersede an explanation of a different line', X + f",supersedes_id) VALUES('x',{CV},'r4','ignored','r','x_full')")
refused('explain: supersede an already superseded explanation', X + f",supersedes_id) VALUES('x',{CV},'r_eur','ignored','r','x_old')")
refused('explain: second successor for one explanation', X + f",supersedes_id) VALUES('y',{CV},'r1','ignored','r','x_full')",
        setup=[X + f",supersedes_id) VALUES('x',{CV},'r1','ignored','r','x_full')"])
refused('explain: booked with no entry', X + f") VALUES('x',{CV},'r4','booked','r')")
refused('explain: ignored that names an entry', X + f",entry_id) VALUES('x',{CV},'r4','ignored','r','e_open')")
refused('explain: unknown kind', X + f") VALUES('x',{CV},'r4','deferred','r')")
refused('explain: blank reason', X + f") VALUES('x',{CV},'r4','ignored','   ')")
refused('explain: a line whose statement has been replaced', X + f") VALUES('x',{CV},'r4','ignored','r')",
        setup=[ST + f",replaces_id) VALUES('s1b',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')"],
        expect='statement_replaced')
allowed('explain: supersede the live explanation (positive control)', X + f",supersedes_id) VALUES('x',{CV},'r1','ignored','r','x_full')")

refused('alloc: zero amount', A + "'x_r4','r4','e_open.p1',0)", setup=[X4])
refused('alloc: sign mismatch vs line', A + "'x_r4','r4','e_spare.p1',5000000)", setup=[X4])
refused('alloc: cross-account (line cash, posting exp)', A + "'x_r4','r4','e_open.p2',5000000)", setup=[X4])
refused('alloc: cross-currency (line USD, posting EUR cash)', A + "'x_r4','r4','e_eur.p1',-5000000)", setup=[X4])
refused('alloc: non-cash account posting', A + "'x_r4','r4','e_noncash.p1',-5000000)", setup=[X4])
refused('alloc: posting of a DRAFT entry', A + "'x_r4','r4','e_draft.p1',-5000000)", setup=[X4])
refused('alloc: quantum violation', A + "'x_r4','r4','e_open.p1',-12345)", setup=[X4])
refused('alloc: -2^63', A + "'x_r4','r4','e_open.p1',-9223372036854775808)", setup=[X4])
refused('alloc: unknown explanation', A + "'nope','r4','e_open.p1',-5000000)")
refused('alloc: unknown line', A + "'x_r4','nope','e_open.p1',-5000000)", setup=[X4])
refused('alloc: unknown posting', A + "'x_r4','r4','nope',-5000000)", setup=[X4])
refused('alloc: explanation of a different line', A + "'x_full','r4','e_open.p1',-5000000)")
refused('alloc: into a superseded explanation', A + "'x_old','r_eur','e_eur.p1',-12000000)")
refused('alloc: into an ignored explanation', A + "'x_ignored','r3','e_open.p1',-5000000)")
refused('alloc: duplicate (explanation, posting)', A + "'x_full','r1','e_posted.p1',-1000000)")
refused('alloc: over-allocate the line', A + "'x_r4','r4','e_open.p1',-6000000)", setup=[X4], expect='overallocated')
refused('alloc: over-allocate the posting', A + "'x_r4','r4','e_posted.p1',-5000000)", setup=[X4], expect='overallocated')
refused('alloc: over-allocate the line across two allocations of one explanation', A + "'x_r4','r4','e_spare.p1',-1000000)",
        setup=[X4, A.replace("'x'", "'y'") + "'x_r4','r4','e_open.p1',-5000000)"])
refused('alloc: UPDATE', "UPDATE allocations SET amount_micros=-11000000 WHERE id='al1'")
refused('alloc: DELETE', "DELETE FROM allocations WHERE id='al1'")
allowed('alloc: a whole line against a whole posting (positive control)', A + "'x_r4','r4','e_open.p1',-5000000)", setup=[X4])
allowed('alloc: a partial allocation is a state, not a refusal', A + "'x_r4','r4','e_open.p1',-2000000)", setup=[X4])

# corrections
CO = f"INSERT INTO corrections(id,{C},original_id,mirror_id) VALUES('x',{CV},"
refused('corrections: duplicate mirror for same original', CO + "'e_posted2','e_rev')")
refused('corrections: same mirror for second original', CO + "'e_posted','e_rev2')")
refused('corrections: mirror not origin=reversal', CO + "'e_posted','e_posted2')")
refused('corrections: mirror is draft', CO + "'e_posted','e_draft')")
refused('corrections: original is draft', CO + "'e_draft','e_rev')")
refused('corrections: original is void', CO + "'e_void','e_rev')")
refused('corrections: original == mirror', CO + "'e_rev','e_rev')")
refused('corrections: replacement is draft', f"INSERT INTO corrections(id,{C},original_id,mirror_id,replacement_id) VALUES('x',{CV},'e_posted','e_rev','e_draft')")
refused('corrections: UPDATE', "UPDATE corrections SET replacement_id='e_jan' WHERE id='cor1'")
refused('corrections: DELETE', "DELETE FROM corrections WHERE id='cor1'")
allowed('corrections: posted original + posted reversal mirror (positive control)', CO + "'e_posted','e_rev')")

# append-only / immutable tables
for t, where in [('events', 'seq=1'),
                 ('evidence_links', "id='ev1'"), ('requests', "id='rq1'"), ('snapshots', "id='sn1'"), ('exports', "id='x1'"),
                 ('statement_lines', "id='r1'"), ('documents', "id='f1'"), ('statements', "id='s1'"),
                 ('explanations', "id='x_full'"), ('allocations', "id='al1'"),
                 ('document_expectations', "id='dx1'"),
                 ('unrecovered_tax', "id='ut1'"),
                 ('commodities', "id='com-USD'"), ('account_commodities', "id='ac-cash-USD'"),
                 ('journal_mappings', "id='jm1'"), ('entities', "id='E'")]:
    refused(f'{t}: UPDATE created_by', f"UPDATE {t} SET created_by='evil' WHERE {where}")
    refused(f'{t}: DELETE', f"DELETE FROM {t} WHERE {where}")
refused('events: UPDATE after', "UPDATE events SET after='{}' WHERE seq=1")
refused('events: INSERT with reused seq', f"INSERT INTO events(seq,{C},kind,subject_kind,subject_id) VALUES(1,{CV},'k','s','i')")
refused('operations: UPDATE terminal state', "UPDATE operations SET state='running', version=2 WHERE id='op1'")
refused('operations: UPDATE intent', "UPDATE operations SET intent='{\"x\":1}', version=2 WHERE id='op2'")
refused('operations: UPDATE without version bump', "UPDATE operations SET state='succeeded' WHERE id='op2'")
refused('operations: DELETE', "DELETE FROM operations WHERE id='op1'")
refused('accounts: DELETE', "DELETE FROM accounts WHERE id='noncash'")
refused('accounts: change path of referenced account', "UPDATE accounts SET path='p2', version=2 WHERE id='cash'")
refused('accounts: change is_cash of referenced account', "UPDATE accounts SET is_cash=0, version=2 WHERE id='cash'")
refused('accounts: change type of referenced account', "UPDATE accounts SET type='liability', version=2 WHERE id='cash'")
refused('accounts: archive without version bump', "UPDATE accounts SET archived=1 WHERE id='noncash'")
refused('evidence_links: two subjects', f"INSERT INTO evidence_links(id,{C},document_id,entry_id,statement_id,locator,caption) VALUES('x',{CV},'f2','e_posted','s1','l','c')")
refused('evidence_links: no subject', f"INSERT INTO evidence_links(id,{C},document_id,locator,caption) VALUES('x',{CV},'f2','l','c')")
# document expectations
DX = f"INSERT INTO document_expectations(id,{C},entry_id,state,awaiting,reason"
refused('expectations: second live awaiting on one entry', DX + f") VALUES('x',{CV},'e_posted','awaiting','another','r')")
refused('expectations: resolution with nothing awaited', DX + f",evidence_id) VALUES('x',{CV},'e_posted2','met','a','r','ev1')")
refused('expectations: withdrawal with nothing awaited', DX + f") VALUES('x',{CV},'e_posted2','withdrawn','a','r')")
refused('expectations: supersede an expectation of a different entry', DX + f",supersedes_id) VALUES('x',{CV},'e_posted2','withdrawn','a','r','dx1')")
refused('expectations: met without the evidence that met it', DX + f",supersedes_id) VALUES('x',{CV},'e_posted','met','a','r','dx1')")
refused('expectations: awaiting names nothing', DX + f") VALUES('x',{CV},'e_posted2','awaiting','   ','r')")
refused('expectations: blank reason', DX + f") VALUES('x',{CV},'e_posted2','awaiting','a','  ')")
refused('expectations: unknown state', DX + f") VALUES('x',{CV},'e_posted2','pending','a','r')")
refused('expectations: second successor for one expectation', DX + f",supersedes_id) VALUES('y',{CV},'e_posted','withdrawn','a','r','dx1')",
        setup=[DX + f",supersedes_id) VALUES('x',{CV},'e_posted','withdrawn','a','r','dx1')"])
allowed('expectations: awaiting on an entry that has none (positive control)', DX + f") VALUES('x',{CV},'e_posted2','awaiting','the invoice','r')")
allowed('expectations: met, superseding the live awaiting row', DX + f",evidence_id,supersedes_id) VALUES('x',{CV},'e_posted','met','a','r','ev1','dx1')")
# Deliberately no period-lock guard (mirroring migration 0004): an expectation has no amount, no
# account and no date of its own, and the month in which somebody realises a booking was provisional is very
# often not the month the booking is in.
allowed('expectations: awaiting on an entry in a locked period', DX + f") VALUES('x',{CV},'e_jan','awaiting','the invoice','r')", **L)

# unrecovered tax
UT = f"INSERT INTO unrecovered_tax(id,{C},entry_id,commodity,amount_micros,state,reason"
refused('unrecovered: second live record for one entry and commodity', UT + f") VALUES('x',{CV},'e_posted','USD',10000,'unrecovered','r')")
refused('unrecovered: negative amount', UT + f") VALUES('x',{CV},'e_posted2','USD',-10000,'unrecovered','r')")
refused('unrecovered: zero amount', UT + f") VALUES('x',{CV},'e_posted2','USD',0,'unrecovered','r')")
refused('unrecovered: amount off the commodity quantum', UT + f") VALUES('x',{CV},'e_posted2','USD',12345,'unrecovered','r')")
refused('unrecovered: resolution with nothing unrecovered', UT + f",recovered_entry_id) VALUES('x',{CV},'e_posted2','USD',10000,'recovered','r','e_jan')")
refused('unrecovered: recovered without the entry that recovered it', UT + f",supersedes_id) VALUES('x',{CV},'e_posted','USD',10000,'recovered','r','ut1')")
refused('unrecovered: forgone naming a recovering entry', UT + f",recovered_entry_id,supersedes_id) VALUES('x',{CV},'e_posted','USD',10000,'forgone','r','e_jan','ut1')")
refused('unrecovered: recovered by the entry it was unrecovered on', UT + f",recovered_entry_id,supersedes_id) VALUES('x',{CV},'e_posted','USD',10000,'recovered','r','e_posted','ut1')")
refused('unrecovered: supersede a record of a different entry', UT + f",supersedes_id) VALUES('x',{CV},'e_posted2','USD',10000,'forgone','r','ut1')")
refused('unrecovered: supersede a record of a different commodity', UT + f",supersedes_id) VALUES('x',{CV},'e_posted','EUR',10000,'forgone','r','ut1')")
refused('unrecovered: posting of another entry', UT + f",posting_id) VALUES('x',{CV},'e_posted2','USD',10000,'unrecovered','r','e_posted.p1')")
refused('unrecovered: unknown state', UT + f") VALUES('x',{CV},'e_posted2','USD',10000,'written_off','r')")
refused('unrecovered: blank reason', UT + f") VALUES('x',{CV},'e_posted2','USD',10000,'unrecovered','   ')")
refused('unrecovered: second successor for one record', UT + f",supersedes_id) VALUES('y',{CV},'e_posted','USD',10000,'forgone','r','ut1')",
        setup=[UT + f",supersedes_id) VALUES('x',{CV},'e_posted','USD',10000,'forgone','r','ut1')"])
allowed('unrecovered: a record on an entry that has none (positive control)', UT + f") VALUES('x',{CV},'e_posted2','USD',10000,'unrecovered','r')")
allowed('unrecovered: the same entry in another commodity', UT + f") VALUES('x',{CV},'e_posted','EUR',10000,'unrecovered','r')")
allowed('unrecovered: recovered, superseding the live record', UT + f",recovered_entry_id,supersedes_id) VALUES('x',{CV},'e_posted','USD',10000,'recovered','r','e_jan','ut1')")
allowed('unrecovered: a leg of the same entry', UT + f",posting_id) VALUES('x',{CV},'e_posted2','USD',10000,'unrecovered','r','e_posted2.p1')")
# No period-lock guard, for the reason 0005 gives: the moment somebody establishes that a tax was not recovered
# is very often long after the month it was not recovered in.
allowed('unrecovered: a record on an entry in a locked period', UT + f") VALUES('x',{CV},'e_jan','USD',10000,'unrecovered','r')", **L)

refused('commodities: bad quantum for precision', f"INSERT INTO commodities(id,{C},code,precision,quantum) VALUES('x',{CV},'GBP',2,1)")
refused('entities: second entity', f"INSERT INTO entities(id,singleton,name,legal_form,country,base_currency,tax_regime,created_at,created_by) VALUES('F',1,'n','l','c','USD','{{}}','t','w')")

# conflict-resolution REPLACE (review D1)
# DELETE triggers do not fire for REPLACE unless PRAGMA recursive_triggers=ON, which the guarded connection sets.
refused('REPLACE: posted entry re-inserted as draft (same id)', f"INSERT OR REPLACE INTO entries(id,{C},dt,narration,status,origin,version) VALUES('e_posted',{CV},'2026-03-10','rewritten','draft','source',1)")
refused('REPLACE: period_locks singleton reset to through=NULL', f"INSERT OR REPLACE INTO period_locks(id,{C},singleton,through,version) VALUES('lock',{CV},1,NULL,1)", locked=True)
refused('REPLACE: live explanation rewritten', f"INSERT OR REPLACE INTO explanations(id,{C},line_id,kind,reason) VALUES('x_full',{CV},'r1','ignored','forged')")
refused('REPLACE: events seq 1 rewritten', f"INSERT OR REPLACE INTO events(seq,{C},kind,subject_kind,subject_id) VALUES(1,{CV},'forged','x','y')")
refused('REPLACE: commodity USD quantum changed', f"INSERT OR REPLACE INTO commodities(id,{C},code,precision,quantum) VALUES('com-USD',{CV},'USD',6,1)")
refused('REPLACE: referenced cash account made non-cash', f"INSERT OR REPLACE INTO accounts(id,{C},path,type,is_cash,archived,version) VALUES('cash',{CV},'path:cash','asset',0,0,1)")
refused('REPLACE: statement line amount rewritten via source_key conflict', f"INSERT OR REPLACE INTO statement_lines(id,{C},statement_id,line_no,source_key,content_hash,dt,amount_micros,description,fields,raw) VALUES('r2',{CV},'s1',2,'k2','rh2','2026-03-11',999990000,'d','{{}}','r')")
refused('REPLACE: correction relation rewritten', f"INSERT OR REPLACE INTO corrections(id,{C},original_id,mirror_id) VALUES('cor1',{CV},'e_posted2','e_rev2')")
refused('REPLACE: close_records rewritten', f"INSERT OR REPLACE INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr1',{CV},'2026-02-28',NULL,'sn1','close','forged','[]')", locked=True)

# review fixes, D1..D5
# D1 defence in depth: even on a connection with recursive_triggers OFF (guard bypassed for the test), the
# four tables with an `exists` check refuse the REPLACE.  Also the other conflict shapes the review probed.
OFF = {"recursive_triggers": "OFF"}
refused('D1: REPLACE posted entry with recursive_triggers OFF still refused', f"INSERT OR REPLACE INTO entries(id,{C},dt,narration,status,origin,version) VALUES('e_posted',{CV},'2026-03-10','rewritten','draft','source',1)", unguarded_pragmas=OFF, expect='exists')
refused('D1: REPLACE live explanation with recursive_triggers OFF still refused', f"INSERT OR REPLACE INTO explanations(id,{C},line_id,kind,reason) VALUES('x_full',{CV},'r1','ignored','forged')", unguarded_pragmas=OFF, expect='exists')
refused('D1: REPLACE period_locks with recursive_triggers OFF still refused', f"INSERT OR REPLACE INTO period_locks(id,{C},singleton,through,version) VALUES('lock',{CV},1,NULL,1)", locked=True, unguarded_pragmas=OFF, expect='exists')
refused('D1: REPLACE events seq with recursive_triggers OFF still refused', f"INSERT OR REPLACE INTO events(seq,{C},kind,subject_kind,subject_id) VALUES(1,{CV},'forged','x','y')", unguarded_pragmas=OFF, expect='exists')
refused('D1: period_locks second row via different id/singleton conflict path', f"INSERT OR REPLACE INTO period_locks(id,{C},singleton,through,version) VALUES('lock2',{CV},1,NULL,1)", locked=True, unguarded_pragmas=OFF, expect='exists')
refused('D1: upsert ON CONFLICT DO UPDATE narration of posted entry', f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('e_posted',{CV},'2026-03-10','n','manual') ON CONFLICT(id) DO UPDATE SET narration='upserted', version=version+1", expect='exists')
refused('D1: upsert ON CONFLICT(entry_id,seq) DO UPDATE amount of posted leg', f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('zz',{CV},'e_posted',1,'cash','USD',-1000000) ON CONFLICT(entry_id,seq) DO UPDATE SET amount_micros=-1000000", expect='parent_or_lock')
refused('D1: UPDATE OR REPLACE draft entry id onto posted id', "UPDATE OR REPLACE entries SET id='e_posted', version=version+1 WHERE id='e_draft'", expect='immutable')
refused('D1: UPDATE OR REPLACE an allocation onto another explanation', "UPDATE OR REPLACE allocations SET explanation_id='x_full', line_id='r1', posting_id='e_posted.p1' WHERE id='al2'", expect='immutable')
refused('D1: UPDATE ... FROM posted entries', "UPDATE entries SET narration=x.narration, version=entries.version+1 FROM (SELECT 'h' AS narration) x WHERE entries.id='e_posted'", expect='immutable')
refused('D1: UPDATE postings ... FROM (posted)', "UPDATE postings SET amount_micros=x.a FROM (SELECT -1000000 AS a) x WHERE postings.id='e_posted.p1'", expect='old_parent_or_lock')

# D2: the over-allocation check is restricted to what the write touches.  Poison r1 -- shrink the line under its
# live allocation -- by temporarily dropping its immutability trigger in setup.  An unrelated allocation must
# still be accepted; one that touches the poisoned line or its posting must still be refused.
POISON = ["DROP TRIGGER statement_lines_u",
          "UPDATE statement_lines SET amount_micros=-10000 WHERE id='r1'",
          "CREATE TRIGGER statement_lines_u BEFORE UPDATE ON statement_lines BEGIN SELECT RAISE(ABORT,'immutable'); END"]
X4P = f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES('x_r4',{CV},'r4','linked','r')"
allowed('D2: an UNRELATED allocation is accepted while another line is over-allocated',
        f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},'x_r4','r4','e_open.p1',-5000000)",
        setup=POISON + [X4P])
refused('D2: an allocation touching the over-allocated posting is refused',
        f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},'x_r4','r4','e_posted.p1',-5000000)",
        setup=POISON + [X4P], expect='overallocated')

# D3 was the settlements period-lock and posted-state guard.  ``settlements`` had no writer and no inbound
# reference, and migration 0003 dropped it (chunk G1), so its seven cases went with the table they guarded.

# D4: stranded drafts.  draft->void is allowed under a lock; nothing else about a locked draft may change.
refused('D4: void locked draft entry while changing narration', "UPDATE entries SET status='void', narration='n', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void locked draft entry while changing dt', "UPDATE entries SET status='void', dt='2026-03-01', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void locked draft entry while changing origin', "UPDATE entries SET status='void', origin='manual', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void locked draft entry without version bump', "UPDATE entries SET status='void' WHERE id='e_jan_draft'", locked=True, expect='version_conflict')
# E1: the note fields join the D4 enumeration.  Voiding a locked draft may change status and version, nothing else.
refused('D4: void locked draft entry while changing comment', "UPDATE entries SET status='void', comment='c', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void locked draft entry while changing code', "UPDATE entries SET status='void', code='C', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void locked draft entry while changing tags', "UPDATE entries SET status='void', tags='[[\"t\",\"1\"]]', version=2 WHERE id='e_jan_draft'", locked=True, expect='period_locked')
refused('D4: void voided locked draft again (void is final)', "UPDATE entries SET status='void', version=3 WHERE id='e_jan_draft'", locked=True,
        setup=["UPDATE entries SET status='void', version=2 WHERE id='e_jan_draft'"], expect='immutable')
refused('D4: postings of a locked draft still immutable after void', "UPDATE postings SET amount_micros=-2000000 WHERE id='e_jan_draft.p1'", locked=True,
        setup=["UPDATE entries SET status='void', version=2 WHERE id='e_jan_draft'"], expect='old_parent_or_lock')
allowed('D4: void locked draft entry (status/version only)', "UPDATE entries SET status='void', version=2 WHERE id='e_jan_draft'", locked=True)
allowed('D4: lock advance over live drafts is allowed', "UPDATE period_locks SET through='2026-12-31', version=2, close_id='crx' WHERE id='lock'",
        setup=[f"INSERT INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('crx',{CV},'2026-12-31',NULL,'sn2','close','year end','[]')"])

# D5: supersession must keep its subject
refused('D5: explanation superseding an explanation of a different line', f"INSERT INTO explanations(id,{C},line_id,kind,reason,supersedes_id) VALUES('x',{CV},'r4','ignored','r','x_full')", expect='supersedes_subject')
refused('D5: statement replacing one of a different account/commodity', f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason,replaces_id) VALUES('x',{CV},'f4','cash-eur','EUR','2026-03-01','2026-03-31',0,0,'l','r','s1')", expect='replaces_subject')
allowed('D5: explanation superseding the same line (positive control)', f"INSERT INTO explanations(id,{C},line_id,kind,reason,supersedes_id) VALUES('x',{CV},'r1','ignored','r','x_full')")
allowed('D5: statement replacing the same account/commodity (positive control)', f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason,replaces_id) VALUES('x',{CV},'f4','cash','USD','2026-03-01','2026-03-31',0,0,'l','r','s1')")

# under-specification resolutions (docs/design/decisions.md)
# U6: -2^63 excluded on every micros column
# U6 cases drop the quantum trigger in setup so the DDL CHECK itself is what refuses (-2^63 % 1 = 0 for 6dp commodities)
refused('U6: statement_lines -2^63', f"INSERT INTO statement_lines(id,{C},statement_id,line_no,source_key,content_hash,dt,amount_micros,description,fields,raw) VALUES('x',{CV},'s1',99,'k99','h99','2026-03-01',-9223372036854775808,'d','{{}}','r')", setup=["DROP TRIGGER statement_lines_quantum_insert"], expect='CHECK')
refused('U6: statements.closing_micros -2^63', f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason) VALUES('x',{CV},'f4','cash','USD','2026-04-01','2026-04-30',0,-9223372036854775808,'l','r')", setup=["DROP TRIGGER statements_quantum_insert"], expect='CHECK')
refused('U6: allocations -2^63', f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('x',{CV},'x_r4','r4','e_open.p1',-9223372036854775808)", setup=[f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES('x_r4',{CV},'r4','linked','r')", "DROP TRIGGER allocations_quantum_insert"], expect='CHECK')

# runner
IDS = [c.name for c in CASES]
assert len(set(IDS)) == len(IDS), "duplicate case names"


def _open(case: Case, templates) -> sqlite3.Connection:
    if case.unguarded_pragmas is None:
        return restore_into_memory(templates[case.locked])
    # Test-only: a deliberately misconfigured connection to prove defence-in-depth triggers hold on their own.
    raw = sqlite3.connect(":memory:", isolation_level=None)
    raw.execute("PRAGMA foreign_keys=ON")
    for k, v in case.unguarded_pragmas.items():
        raw.execute(f"PRAGMA {k}={v}")
    src = sqlite3.connect(templates[case.locked])
    try:
        src.backup(raw)
    finally:
        src.close()
    return raw


def _run_setup(con: sqlite3.Connection, case: Case) -> None:
    for s in case.setup:
        try:
            con.execute(*s) if isinstance(s, tuple) else con.execute(s)
        except Exception as e:  # noqa: BLE001
            raise FixtureSetupError(f"SETUP-ERROR in case {case.name!r}: {s!r} -> {e}") from e


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_mutation_matrix(case: Case, templates):
    con = _open(case, templates)
    try:
        _run_setup(con, case)
        if case.expect_refused:
            # Only a constraint-class refusal counts.  A malformed statement (OperationalError: syntax error,
            # no such column...) must not be mistaken for the database refusing the mutation.
            with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError, OverflowError)) as ei:
                try:
                    con.execute(case.sql, case.params)
                except sqlite3.OperationalError as e:
                    if "integer overflow" in str(e):  # SQLite reports arithmetic overflow as OperationalError
                        raise
                    raise FixtureSetupError(f"MALFORMED-CASE {case.name!r}: {e}\nSQL: {case.sql}") from e
            if case.expect is not None:
                assert case.expect in str(ei.value), f"refused for the wrong reason: {ei.value!r} (expected {case.expect!r})"
        else:
            try:
                con.execute(case.sql, case.params)
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"positive control refused: {e}\nSQL: {case.sql}")
    finally:
        con.close()


def test_matrix_size_did_not_shrink():
    """The matrix may only shrink when the thing a case guarded stops existing, and then by exactly that much.

    Cases have gone only with their subjects: chunk G1 (four tables with no writer, reader or inbound reference),
    issue 0002 (``books`` and every ``book_year`` column, migration 0008), issue 0004 (the statement/matching
    schema, migration 0009, whose rules all have cases on the tables that replaced them) and issue 0005 (the
    approval chain, migration 0010, replaced by the ``close_records.overrides`` cases).  Migration 0004 moved
    ``evidence_links -> locked entry`` from refusal to positive control, because filing a document against an
    entry changes nothing a closed period reports (docs/design/decisions.md, "Documents that are not bank
    statements"); links to a locked assertion or opening are still refused.

    The floor is re-measured, not derived.  What it enforces is that the matrix cannot quietly shrink from here.
    """
    n_refused = sum(1 for c in CASES if c.expect_refused)
    n_allowed = sum(1 for c in CASES if not c.expect_refused)
    assert n_refused >= 265, n_refused
    assert n_allowed >= 33, n_allowed


def test_setup_failures_are_reported_distinctly(templates):
    bad = Case("probe", "SELECT 1", setup=["INSERT INTO nowhere VALUES(1)"])
    con = restore_into_memory(templates[False])
    try:
        with pytest.raises(FixtureSetupError, match="SETUP-ERROR"):
            _run_setup(con, bad)
    finally:
        con.close()


# sequences that need more than one statement
def test_d4_stranded_drafts_can_be_voided_after_lock_advance(db):
    """The review's stranded-drafts scenario end to end: lock jumps to year end over live drafts; every draft can
    still be voided, none can be sealed, and the lock never had to wait."""
    ins(db, "close_records", id="crx", through="2026-12-31", prior_through=None, snapshot_id="sn2",
        kind="close", reason="year end", overrides="[]")
    db.execute("UPDATE period_locks SET through='2026-12-31', version=2, close_id='crx' WHERE id='lock'")
    drafts = [r[0] for r in db.execute("SELECT id FROM entries WHERE status='draft'")]
    assert drafts
    with pytest.raises(sqlite3.IntegrityError, match="period_locked"):
        db.execute("UPDATE entries SET status='posted', version=2 WHERE id=?", (drafts[0],))
    for eid in drafts:
        db.execute("UPDATE entries SET status='void', version=2 WHERE id=?", (eid,))
    assert db.execute("SELECT count(*) FROM entries WHERE status='draft'").fetchone()[0] == 0
    # and void is final
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE entries SET status='draft', version=3 WHERE id=?", (drafts[0],))


def test_overallocated_view_still_reports_globally_for_the_verifier(db):
    """D2 keeps the global view for the verifier: poison a line and the view must list it, and the posting its
    allocation points at with it -- both ends of an over-allocation are over-allocated."""
    for s in POISON:
        db.execute(s)
    assert [r[0] for r in db.execute("SELECT id FROM overallocated")] == ["r1"]


def _bulk_allocations(con: sqlite3.Connection, n: int, prefix: str) -> None:
    """``n`` fully explained lines, each with its own posted entry and one live allocation.

    ``line_no`` counts from the index and not from ``hash(k)``: string hashing is salted per process, so the
    same n draws a different 10**8 spread every run and 2000 of them collide under UNIQUE(statement_id,line_no)
    about one run in fifty.  The value is only required to be unique within the statement, which the index is.
    """
    con.execute("BEGIN")
    for i in range(n):
        k = f"{prefix}{i}"
        ins(con, "statement_lines", id=f"R{k}", statement_id="s1", line_no=100000 + i,
            source_key=k, content_hash=f"H{k}", dt="2026-03-25", amount_micros=-10000, description="d",
            fields="{}", raw="r")
        ins(con, "entries", id=f"E{k}", dt="2026-03-25", narration="n", origin="source")
        ins(con, "postings", id=f"E{k}.1", entry_id=f"E{k}", seq=1, account_id="cash", commodity="USD", amount_micros=-10000)
        ins(con, "postings", id=f"E{k}.2", entry_id=f"E{k}", seq=2, account_id="exp", commodity="USD", amount_micros=10000)
        con.execute("UPDATE entries SET status='posted', version=2 WHERE id=?", (f"E{k}",))
        ins(con, "explanations", id=f"X{k}", line_id=f"R{k}", kind="linked", reason="r")
        ins(con, "allocations", id=f"A{k}", explanation_id=f"X{k}", line_id=f"R{k}", posting_id=f"E{k}.1",
            amount_micros=-10000)
    con.execute("COMMIT")


@pytest.mark.slow
def test_allocation_cost_does_not_grow_with_allocated_history(fresh_db):
    """The property D2 established, on the model that replaced match sets (issue 0004).

    ``overallocated`` is a view over *every* live allocation, and the AFTER INSERT guard consults it -- so the
    guard has to be restricted to the line and the posting the write actually touches, or every allocation would
    get slower for the rest of the entity's life.  Writing an allocation with 2000 of them already stored must
    cost about what it costs with 100.  Generous 6x bound keeps the test stable under load.
    """

    def allocation_cost(n: int) -> float:
        con = fresh_db()
        _bulk_allocations(con, n, "a")
        con.execute("BEGIN")
        for i in range(20):
            k = f"t{i}"
            ins(con, "statement_lines", id=f"R{k}", statement_id="s1", line_no=900000 + i, source_key=k,
                content_hash=f"H{k}", dt="2026-03-26", amount_micros=-10000, description="d", fields="{}", raw="r")
            ins(con, "entries", id=f"E{k}", dt="2026-03-26", narration="n", origin="source")
            ins(con, "postings", id=f"E{k}.1", entry_id=f"E{k}", seq=1, account_id="cash", commodity="USD", amount_micros=-10000)
            ins(con, "postings", id=f"E{k}.2", entry_id=f"E{k}", seq=2, account_id="exp", commodity="USD", amount_micros=10000)
            con.execute("UPDATE entries SET status='posted', version=2 WHERE id=?", (f"E{k}",))
            ins(con, "explanations", id=f"X{k}", line_id=f"R{k}", kind="linked", reason="r")
        con.execute("COMMIT")
        t0 = time.perf_counter()
        con.execute("BEGIN")
        for i in range(20):
            k = f"t{i}"
            ins(con, "allocations", id=f"A{k}", explanation_id=f"X{k}", line_id=f"R{k}", posting_id=f"E{k}.1",
                amount_micros=-10000)
        con.execute("COMMIT")
        return (time.perf_counter() - t0) / 20

    small, large = allocation_cost(100), allocation_cost(2000)
    print(f"\nallocation cost: {small*1000:.3f} ms with 100 allocations, {large*1000:.3f} ms with 2000")
    assert large < max(6 * small, 0.002), f"allocation cost grew from {small*1000:.3f} ms to {large*1000:.3f} ms"


@pytest.mark.parametrize("table,sql", [
    ("close_records", f"INSERT OR REPLACE INTO close_records(id,{C},through,prior_through,snapshot_id,kind,reason,overrides) VALUES('cr1',{CV},'2026-02-28',NULL,'sn1','close','forged','[]')"),
])
def test_recursive_triggers_pragma_is_load_bearing(templates, table, sql):
    """Documents why the guard exists: for tables without an `exists` check, REPLACE succeeds when
    recursive_triggers is OFF and is refused when it is ON.  If this test ever fails on the OFF side, SQLite
    changed behaviour and the defence-in-depth list can be revisited."""
    off = _open(Case("off", sql, locked=True, unguarded_pragmas={"recursive_triggers": "OFF"}), templates)
    try:
        off.execute(sql)  # fails open without the pragma
    finally:
        off.close()
    on = restore_into_memory(templates[True])
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            on.execute(sql)
    finally:
        on.close()
