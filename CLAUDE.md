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

## Testing against Gmail

This project's tests talk to a live mailbox, so testing has consequences the
test suite does not.

- **Do not make bursts of IMAP connections.** No benchmark loops, no
  reconnecting once per command. Take a few samples, spaced out, and reuse one
  connection for multiple operations. After ~35 logins inside 20 minutes, two
  subsequent real deliveries stalled past a 60s socket timeout; causation was
  never proven, but spacing tests out costs nothing.
- **Live APPEND tests go to `"[Gmail]/Trash"`,** not INBOX - the message is
  real, it auto-purges, and it never clutters the inbox.
- **Re-uploading a message is safe.** APPEND preserves `Message-ID`, so Gmail
  dedupes rather than duplicating. Recovering a failed upload needs no
  bookkeeping.
- **Measurement traffic pollutes the production sample.** Anything measured
  here also runs for real, so leave a quiet period before reading conclusions
  into failure counts.
- **Gmail's IMAP latency is heavy-tailed, not slow.** Typical round trips are
  a fraction of a second, but occasional session-wide stalls of tens of
  seconds are normal. Never characterise it from one sample.

## Care

These scripts run inside a mail delivery path, where an unhandled exception
costs a copy of someone's mail. Prefer failing loudly over failing silently,
and prefer returning the input unchanged over raising.

A measured maximum is not a bound. A 60s timeout chosen as "4x the worst
observed" was exceeded by real traffic within the hour.
