# Haddon and other open-source accounting tools

Haddon is new, and the tools on this page are not. hledger, Beancount, Ledger and GnuCash have kept people's
books for between one and nearly three decades, and each does something better than Haddon does. This page
says what each is good at, how Haddon differs, and when you would pick one over the other. If something here
is out of date or unfair, please open an issue.

Haddon builds on hledger rather than competing with it. It imports hledger journals, exports to them, and checks
every export with a pinned hledger binary before publishing it ([hledger.md](hledger.md)). Books kept in Haddon
can always be read, reported on and kept going in hledger.

## At a glance

| | Haddon | hledger | Beancount and Fava | Ledger | GnuCash |
|---|---|---|---|---|---|
| Storage | one SQLite file per entity | plain-text journal files | plain-text files | plain-text journal files | XML file, or SQLite, MySQL or PostgreSQL |
| How entries are written | named HTTP commands, validated before they land, each recorded with an actor and a reason | edit the journal; `add`, `import`, or the hledger-web form | edit the files; importers (beangulp); Fava's editor and entry form | edit the journal; Ledger never changes your files | desktop forms and registers, importers, Python bindings |
| Change history | append-only audit log; posted entries can't be edited, only reversed or corrected | the files are text, so version control | the files are text, so version control | the files are text, so version control | per-session `.log` files for recovery, and backups |
| Closing a period | a period lock, a checked close with recorded overrides, a recorded reopen | `close` generates closing and opening entries | `close` closes an account; queries can summarise a period | `equity` prints balances to start a new file | a read-only threshold (transactions older than N days), and a Close Book tool |
| Several entities | one service, one file per entity, tokens scoped to entities | a journal per set of books | a ledger per set of books | a journal per set of books | a data file per book |
| Bank statements | the agent submits each statement's lines and balances; every line is booked, linked or ignored; per-account reconciliation | CSV rules files; balance assertions | importers; `balance` assertions | `convert` for CSV; balance assertions | QIF, OFX, CSV and AqBanking import; a reconcile window |
| Programmatic access | an HTTP API described by OpenAPI, with an example of turning it into agent tools | hledger-web's JSON API; JSON and CSV output | a Python API, plugins, and the BQL query language | Python bindings | Python bindings, where built in |
| Currencies | balances kept per currency, no conversion or revaluation | market prices and valuation | prices and cost lots | commodity prices and valuation | a price database and currency conversion |
| Reports | trial balance, income statement, balance sheet with a comparative period (JSON or PDF), registers | many, with a query language | BQL; Fava shows the statements and charts | many, with value expressions | standard and custom reports, charts |
| Web interface | included, for browsing the books, correcting entries and closing periods | hledger-web, and a terminal UI | Fava | none bundled | the desktop application |
| Licence, language, since | MIT, Python, 2026 (pre-1.0) | GPL-3.0+, Haskell, 2007 | GPL-2.0, Python, 2007; Fava MIT, 2016 | BSD-3-Clause, C++, 2003 | GPL, C and C++, 1998 |

## hledger

hledger is a fast, careful plain-text accounting tool. Its journal format is readable and diffable, its checks
(`hledger check`, balance assertions) are thorough, and CSV rules files turn bank exports into entries you can
keep in version control. hledger-web adds a browser view and a small JSON API.

Pick hledger if you, or a script you trust, keep the books, and you want them as text you can edit, grep and
review in git. Haddon's own journal format is hledger's, so the two work together: import an existing hledger
ledger into Haddon when you want an agent to take over the data entry, and export from Haddon whenever you
want hledger's reports or an independent check.

## Beancount and Fava

Beancount is plain-text accounting with a strict model: accounts are opened and closed explicitly, balance
assertions are checked, and holdings are tracked at cost, lot by lot, which makes it a strong choice for
investments. Its Python API and plugins make it programmable, and BQL gives you SQL-like queries over the
books. A `document` directive ties statement files to accounts. Fava is its web interface, for browsing reports,
editing the source, adding entries, running importers and attaching documents.

Pick Beancount and Fava if you want strict plain-text books with good investment tracking and a pleasant web
view, and you are happy to keep the files yourself.

## Ledger

Ledger is the original command-line plain-text accounting tool, which hledger and Beancount both learned from. It
reads your journal and never writes to it, and its value expressions make its reports very flexible. Pick it
if you want that flexibility and are comfortable building your own workflow around a text file and an editor
(Emacs's ledger-mode is a common companion).

## GnuCash

GnuCash is a desktop application with a long history and a broad feature set. It imports OFX, QIF and CSV
files and connects to banks through AqBanking, has a reconcile window that works against a statement's ending
balance, lets you link files to transactions, and includes small-business features: invoices, customers and
vendors, and budgets. A book option makes transactions older than a set number of days read-only.

Pick GnuCash if people will enter the transactions themselves in a desktop application, especially for a
small business that needs invoicing, and nobody wants to work in text files.

## When Haddon fits

Haddon is a double-entry ledger built for agents. Every write is a named HTTP command that is validated, made
idempotent and recorded with who made it and why. Posted entries are corrected, not edited. A closed period
stays closed until someone reopens it and says why, and each statement line is explained one way or another
before a month can close without an override. One service holds any number of entities, each in its own file,
and every export is checked by hledger. These are things the tools above leave to whoever edits the files,
which is a fine choice when that is a careful person and a risky one when it is a model. Haddon also comes with
a web interface for browsing the books, correcting entries and closing periods, if you want one.

It is also the youngest tool here and it is pre-1.0. It does fewer things: no currency conversion or revaluation,
no invoicing or bank feeds, and no country's tax rules. If you want to keep
your own books by hand, hledger, Beancount, Ledger or GnuCash will serve you better. If an agent is doing the
data entry for one or more companies, Haddon gives it an API it can use safely and leaves a record of everything it did.
