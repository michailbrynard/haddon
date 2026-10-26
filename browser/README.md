# The reviewer workflow, in a real browser

```
make browser-check          # builds the bundle, then drives it
bin/haddon-browser-check    # the same, assuming `make web` is current
bin/haddon-browser-check -k close --headed --slowmo 300   # watch it
```

Each test is a sequence a reviewer actually walks rather than a page-by-page sweep. The two this page is mostly
about:

`test_reviewer_workflow.py` walks a month. Open a month → read what is unexplained → reach a line, its entry
and the document behind it → close the period → write again without reloading.

`test_statement_replacement.py` replaces a statement. Open the statements → find one that cannot be
replaced and see which line holds it → go to that line → be refused for loading a document that is already
loaded, and reach the statement it belongs to → replace one properly → read the chain from both ends.

Those paths are chosen because they are the sequences where every layer has to agree at once: the service's
state, the query cache holding it, and what the page says about it. Everything outside them (the chart, the
reports, the trial balance) is a read of one endpoint rendered once, and `tests/` already covers those
endpoints far better than a browser can.

The replacement check brings its own books and its own server. That isn't duplication for its own sake. The
month check ends with March and April closed, and a replacement into a locked period is refused before
anything else is looked at. Sharing one fixture would mean the order two files happen to run in decided
what the second one was asserting about. So `page` is bound to the browser only, and each test asks for the
server it wants.

Three more follow the same pattern. `test_review_inbox.py` checks that the inbox says what the service
says, before and after a write. `test_manual_entry.py` writes an entry no statement line stands behind,
including a post that gets refused, and checks the form isn't offered where the command isn't published.
`test_export_journal.py` exports the journal and downloads a file through the link the page built, hashing it
against the digest shown beside it.

## What only a browser can catch here

The Python suite proves the service's answers. `tsc` proves the page compiles. Neither can see the layer in
between, where a write lands and the page still shows what it knew before. A stale balance on screen is
exactly the kind of bug a ledger can't afford, and it type-checks fine.

The concrete instance this harness was built around: `useEntityMutation` invalidates both `["entity", id]`
*and* `["entities"]`, because the entity listing is where `lock_through`, `lock_version` and `lock_close_id`
come from. Drop the second invalidation and a close leaves the page holding the lock version it had before,
so the *next* write from that page is refused as stale against a lock the page itself just moved. Nothing but a
browser doing two writes in a row sees it. The last step of the test is that second write.

The replacement check covers the other half of the same problem: a refusal whose `details` are a list of ids,
and whether those ids became links a person can follow. A screen that renders "2 line(s) of statement
stm_… are still booked" as a red box type-checks perfectly, passes every Python test, and leaves a reviewer
with nowhere to click. So the check follows them: it clicks through from the refusal to the line that caused
it, and from a document-already-loaded refusal to the statement it belongs to.

Its other assertion of the same kind is the one write it does: after a replacement lands, the page has to be
showing the statement it just wrote *and* the predecessor has to have stopped saying it is live. Both are
reads across one write, and both look normal when they are wrong.

## Not under `tests/`

`pyproject.toml` sets `testpaths = ["tests"]`, so the commit gate collects `tests/` and nothing else. This
lives outside it on purpose, because it needs a built bundle and a downloaded browser and the gate has to run
on a fresh checkout. `make test-gate` is unchanged; this is its own command.

`tests/test_tier_completeness.py` is unaffected for the same reason: it collects the suite, and the suite is
`tests/`.

## Setup

```
.venv/bin/pip install -e '.[browser]'
.venv/bin/playwright install chromium
```

## The fixtures

`world.py` builds a fresh entity through the real repository API on every run: the suite's synthetic company,
one obviously-fake bank statement, two lines booked, one excluded with a reason, two left needing an account.

`statements_world.py` does the same for the replacement check, with two statements in the two states that
screen exists to tell apart: an EUR one whose parse stopped at a page break, so it holds three of its
document's four rows and nothing is booked against it; and a USD one with a line booked, which is what the
service refuses a replacement over.

Nothing is shared with `tests/`, nothing persists between runs, and nothing points at a real database; each
harness is handed a temporary file and given a port of its own.
