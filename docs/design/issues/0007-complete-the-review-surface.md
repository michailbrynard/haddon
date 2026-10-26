# 0007 — Complete the human review surface

**Kind:** UI + evidence access + workflow completion  
**Risk:** medium  
**Effort:** L

## Problem

The UI is useful for browsing months, accounts, statement lines and reports, but it is not yet a complete
review surface for the journal:

- there is no free-standing entry creation/review workflow;
- an entry has no dedicated page showing its full audit history and correction chain;
- some split-booking paths tell the reviewer to use the API;
- evidence shows a hash and server-local blob path rather than viewable/downloadable content;
- statement replacement is unavailable; and
- “Post them” dispatches independent statement requests with `Promise.all`, so the wording suggests an atomic
  batch while partial success is possible.

These gaps matter more than additional report polish. The UI’s main job is to let a person understand and
correct what the agent did.

## Desired reviewer loop

From a period or inbox, a reviewer can:

1. see what is unexplained, recently changed, awaiting evidence or blocking close;
2. open the associated statement line, entry, account and source document;
3. inspect postings, actor, reason, timestamps, source hashes and the complete correction/classification chain;
4. create a manual adjustment or request a typed correction/reversal; and
5. confirm that the period is reconciled and close it.

## Work

1. Add an entry route/page with balanced postings, source links, audit timeline, predecessor/successor links and
   clear “posted”, “corrected” or “reversed” state.
2. Add manual entry and correction/reversal dialogs using the same command schemas as agents. Resolve account
   paths with search/autocomplete and display amounts in commodity precision.
3. Complete split classification and statement replacement in the UI. Never direct a reviewer to craft an API
   call for a supported accounting task.
4. Add an authenticated evidence endpoint that streams or safely redirects to content after entity and actor
   authorization. Use content disposition, media type and hash verification; never expose server-local paths.
5. Make batch semantics honest. Either add a server-side atomic batch command, or show each independent result
   and label the action accordingly. A failed item must not make successful items appear unposted or vice
   versa.
6. Add a compact review queue based on real states already in the extensions. Do not invent a parallel generic
   task framework.

## Acceptance criteria

- Every posted entry is reachable from register, account, statement allocation and audit links.
- A reviewer can see who performed each business action, why, and what it superseded.
- Authorized evidence is viewable/downloadable; responses do not disclose filesystem paths.
- Manual posting, correction/reversal, split classification, statement replacement and close require no
  command-line/API workaround.
- Batch actions are atomic or report durable per-item outcomes and safe retry keys.
- Keyboard navigation, loading/error states and destructive confirmations are covered for the core review
  loop.

## Pitfalls

1. The UI must not edit posted rows in place; correction commands preserve history.
2. Evidence access needs authorization and safe content headers even in a single-entity-file deployment.
3. Avoid making every audit record equally prominent. Lead with the business action and offer technical IDs on
   demand.
4. Do not duplicate statement/report shaping rules in React if the server can return the canonical view.
