# Security policy

## Reporting a vulnerability

Please do not report security problems in public issues.

<!-- TODO(maintainer): choose the private reporting route before publishing. Either enable GitHub's private
     vulnerability reporting (Settings > Code security > Private vulnerability reporting) and keep the
     paragraph below, or replace it with a monitored security contact address. -->

Report vulnerabilities privately through this repository's **Security** tab, using **Report a vulnerability**.
Include what an attacker can do, the steps or a proof of concept, the version or commit you tested, and your
configuration if it matters (with secrets removed).

We'll acknowledge your report within a few working days and keep you posted while we work on a fix. Unless
you'd rather we didn't, we'll credit you in the release notes.

## Supported versions

Haddon is pre-1.0. Security fixes are made on the `main` branch only.

## Scope and deployment assumptions

Some of Haddon's guarantees depend on how a deployment is configured, so the most useful reports show a
problem under the setup documented in [docs/operations.md](docs/operations.md). That setup assumes:

- The service binds to loopback and is reached only through a reverse proxy.
- `proxy_auth` trusts a request header to name the person a proxy authenticated. It is safe only when nothing
  can reach the service without passing through that proxy, and when the proxy blanks the header on the
  bearer-token path. A forged header from a client that can reach the port directly is a configuration issue,
  not a vulnerability in Haddon.
- The configuration file holds bearer tokens and cursor-signing keys and must be readable only by the service
  user.
- Document bytes are served only from `blob_root`, exports are written and served only under `export_root`,
  and new databases are created only under `entity_root`. A way to read or write outside those directories is
  in scope.

Also in scope are bypassing authentication or entity scoping, writing to a closed period, altering a posted
entry, defeating idempotency so that a request is applied twice, and getting the service to accept amounts or
records that the documented validation should refuse.

Haddon validates the structure and arithmetic of what an agent writes, but it can't tell whether a
well-formed entry is the right one. An LLM agent misled by a document it was asked to read (prompt injection)
is a risk to manage in the agent's harness. The guardrails in [docs/agent-guide.md](docs/agent-guide.md) are a
starting point.
