# Contributing

Thanks for your interest in improving agent-console. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) first.

## Ground rules

- **The runner's allow-list is the trust boundary.** A new action needs a
  fixed target list, a strict value pattern, a fixed argument list (never a
  shell), and tests showing that bad targets and values are refused.
- **Names, never values.** Nothing that reads `.env` files, templates or
  logs may put a secret value into `status.json`, the audit log or the page.
  `tests/test_collector.py` plants values and checks that none leak; extend
  it when you read a new source.
- **No site specifics in the code.** Paths, names, descriptions and links
  belong in `console.toml` or `catalog.json`. CI runs
  `scripts/scan-private.sh`.
- **Standard library only** on the host. The container adds `cryptography`
  for passkeys.
- **One file for the page.** `console/index.html` has no build step and
  makes no external requests.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                       # needs node for the page smoke tests
ruff check .
scripts/scan-private.sh
```

CI runs the scan, ruff, the tests on Python 3.11 and 3.13, and an image
build; it must be green before a PR can merge.

## Pull requests

1. Fork, create a branch, and make your change with a minimal diff.
2. Add or update tests for the behavior you changed.
3. Open a PR against `main` describing what changed and how you tested it.
   Say explicitly if it changes what an action may do.
