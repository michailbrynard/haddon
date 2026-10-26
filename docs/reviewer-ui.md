# The web interface

Haddon comes with a web interface where you can browse the books, review and correct entries and close
periods, if you want to. Nothing requires it: an agent can do all of the same over the API. It is a React
single-page app (`web/`), built into the Python package and served by the same process as the API.

It has no logic of its own. Every figure it shows comes from `/v1`, and every change it makes is one of the
named commands an agent calls, recorded under the reviewer's own actor name (`ui:<name>`). So anything a
reviewer can see, an agent can query, and what a reviewer does shows up in the audit log next to what the
agents did.

## Pages

| Tab | Path | What it is for |
|---|---|---|
| Review | `/e/{entity}/month?month=YYYY-MM` | One month: does the bank agree with the books, and what still needs a decision. |
| Inbox | `/e/{entity}/review` | Everything unfinished, across all periods. |
| Reports | `/e/{entity}/reports` | Income statement and balance sheet for a period, with a PDF download, and recoverable tax not recovered. |
| Accounts | `/e/{entity}/accounts` | The chart with balances; each account opens its register. |
| Trial balance | `/e/{entity}/trial-balance` | Every account's balance, and whether each commodity nets to zero. |
| Closing | `/e/{entity}/close?through=YYYY-MM-DD` | Every close check as the service evaluated it; close, override, reopen. |

From those, a reviewer reaches an entry (`/entries/{id}`, with its audit timeline and correction chain), a
statement line (`/lines/{id}`, back to the document it was read from and forward to what was booked for it),
the statements list with replacement, and a form for a manual entry.

### Review: one month

The month page starts with whether the bank agrees with the books: the two balances from
`GET /reconciliation` and the difference between them. When the difference is exactly the lines still
unexplained, the page says so.

Below that is everything that wants a decision, such as lines that need an account, lines only partly
explained, days no statement covers and bookings waiting on a document. Each item comes with its action, so a
person can finish it from the page by giving a line an account, moving a posted booking to another account or
excluding a line with a reason.

The close checks are deliberately not on this page. They report over the whole window since the last close,
which can span many months, and most of what they say is not actionable from one month's view. They have their
own page.

### Inbox

The inbox puts every list that means "not finished" in one place: unexplained statement lines, entries
awaiting a document, recoverable tax not recovered, the checks blocking the next close, and recent changes. It
stores nothing itself. Each section is an existing API read without the month filter, so it can't disagree
with the rest of the service.

### Closing

Each check is shown with its status. `unevaluable` is drawn differently from `fail`, because "could not tell"
is a weaker claim than "here is what is wrong", and treating them alike invites overriding a check that found
nothing. The page offers to close only when `close_period` is among the writes this credential's
`/capabilities` lists.

## What each edit asks for

The interface asks for whatever explanation couldn't be reconstructed later, and no more.

A new booking takes an account and a description. The description is recorded both as the entry's narration
and as the reason, and nothing is filled in on the reviewer's behalf. Changing a posted booking asks for one
more sentence, saying why the earlier one was wrong. It goes through `reexplain_line`, which supersedes the
line's explanation and corrects the entry it booked in one transaction. Excluding a line and closing over a
failing check each take a sentence.

There is no approval step and no second signature. The record shows who did what and why, and that's all it
does.

## Running it locally

The interface reaches the API at `/ui/v1`, which a reverse proxy maps to `/v1` after authenticating the
person. Locally, the Vite dev server plays the proxy's part.

1. Add `proxy_auth` to the service configuration, so the service accepts a user name from a header:

   ```json
   "proxy_auth": {"header": "X-Haddon-User"}
   ```

   With this set, anything that can reach the service's port can claim to be anyone, so only use it on
   loopback or behind a proxy that sets the header itself.

2. Start the service, then the dev server:

   ```bash
   bin/haddon-serve --config var/haddon.json      # 127.0.0.1:8080
   make web-dev                                   # Vite on http://localhost:5173
   ```

   `make web-dev` proxies `/ui/v1` to `http://127.0.0.1:8080` with `X-Haddon-User: dev`. Set `HADDON_API` to
   point it at another address, and `HADDON_DEV_USER` to act under another name.

3. Open `http://localhost:5173/`.

`make web` builds the production bundle into `src/haddon/ui/static/` (gitignored), which `haddon-serve` then
serves at `/`. Without a bundle the service still serves the API, and `/` shows a page saying which command to
run.

## Credentials in production

There are two ways in, with the same API behind both:

- `/v1/` takes a bearer token. The proxy blanks the user header on this path, so a token holder cannot also
  name itself as a person.
- `/` and `/ui/v1/` require the proxy's own login. The proxy sets the user header from the identity it
  established, and strips `/ui` so the service sees an ordinary `/v1` request. `proxy_auth` turns that header
  into an actor string, `ui:<name>`, scoped to the entities it lists.

`deploy/haddon.nginx.conf.example` is a worked example using HTTP basic auth; any authenticating proxy works the
same way. See [operations.md](operations.md#deploying-behind-a-reverse-proxy).

## Browser tests

`make browser-check` builds the bundle and drives the main reviewer paths in headless Chromium against a real
server: a month from unexplained lines to a closed period followed by another write, replacing a statement,
the review inbox, writing a manual entry and downloading an export.
It catches what neither the API tests nor the type checker can: a page still showing state from before a write.
It needs Playwright (`make install-browser`) and is not part of the commit gate. See
[browser/README.md](../browser/README.md).
