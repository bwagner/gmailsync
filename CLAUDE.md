# mailsync

Uploads incoming mail to Gmail over IMAP, driven by procmail.

## This repo is public

Before committing any file, check it carries none of:

- server hostnames or internal domains
- the name of any other project
- real email addresses - use `example.test`
- paths, log excerpts or details specific to one machine's setup

That applies to code, comments, docstrings, test fixtures and commit messages
alike. Operational and local context belongs outside this repo.

## Conventions

- Stdlib only. No `pyproject.toml` and no declared dependencies - each script is
  deployed by copying a single file, and a declared test dependency would break
  that. pytest is supplied at run time by uv.
- Tests: `uv run --with pytest pytest -q`
- Scripts carry a `#!/usr/bin/env -S uv run --script` shebang and an empty
  `dependencies = []` block.
- Keep a pure, testable core separate from the thin `__main__` that wraps it.

## Care

These scripts run inside a mail delivery path, where an unhandled exception
costs a copy of someone's mail. Prefer failing loudly over failing silently,
and prefer returning the input unchanged over raising.
