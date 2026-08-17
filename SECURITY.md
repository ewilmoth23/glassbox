# Security policy

Glassbox ingests untrusted third-party data over the network and writes it to a
database that other components query. Treat every ingester as a parsing boundary.

## Supported versions

Pre-1.0. Only the tip of `master` receives fixes.

| Version | Supported |
|---|---|
| `master` | Yes |
| anything older | No — update to `master` |

## Reporting a vulnerability

**Please do not open a public issue.** Email **ethancolewilmoth@gmail.com** with:

- the affected boundary (which ingester, writer, route, or algorithm)
- a safe reproduction that does not require real credentials
- what an attacker gets if it works
- a remediation idea, if you have one

Expect an acknowledgement within a week. This is a one-person project, so a fix
may take longer than that; you will be told either way.

## Known posture, stated plainly

These are design decisions, not undiscovered bugs. Reporting them is not necessary.

- **No authentication layer.** Glassbox is single-operator by design and is not
  intended for public network exposure without one in front of it.
- **Single-node deployment.** No multi-tenancy, no per-tenant isolation.
- **Ingested content is untrusted by definition.** It is normalised and stored, not
  executed, but downstream consumers should treat stored properties as attacker
  influenced.

## What is enforced

The license gate in `sources_registry.py` refuses to start any source lacking a
documented `commercial_use_ok` basis in `infra/sources.yaml`. Bypassing that gate
is a security-relevant change and will be treated as one.
