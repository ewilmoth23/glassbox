# Contributing to Glassbox

Thanks for looking. Glassbox is a personal project published so the work can be
read and run, not a supported product, so set expectations accordingly: issues
get answered when time allows, and large unsolicited pull requests may not get
merged.

## Before you open a pull request

Run the suite. `pytest` should be green before and after your change.

```bash
pip install -r requirements.txt
pytest
```

Keep branches short and named for what they do: `fix/dark-ship-cohort-window`,
`feat/ingester-eumetsat`, `docs/postgres-setup`.

## Adding an ingester

Every source subclasses `ingesters/base.Ingester` and implements `fetch()` and
`normalize()`. Two things are not optional:

1. **A `infra/sources.yaml` entry with real license evidence.** A source without a
   documented `commercial_use_ok` basis will not start, and that is deliberate.
   If you cannot point at the terms that permit commercial use, set the flag false
   and give a `disabled_reason`.
2. **Tests that do not hit the network by default.** Mark anything requiring a live
   connection with `@pytest.mark.network` so it stays out of the default run.

## Changing a correlation algorithm

Detection code that has never been checked against ground truth is a demo. If you
change how an algorithm fires, say in the pull request what the false-positive
behaviour was before and after, and how you know. `docs/ALGORITHM_FP_AUDIT_*.md`
shows the format and the standard.

## What will get a pull request declined

Real credentials or API keys in fixtures. A new source without license evidence.
Detection changes with no false-positive reasoning. Claims that a command passed
when it was not run.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
