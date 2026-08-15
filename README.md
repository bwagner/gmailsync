# gmailsync

Uploads incoming mail to Gmail via IMAP, triggered by procmail. A replacement for Gmail's discontinued ["fetch mail from other accounts"](https://support.google.com/mail/answer/16604719) feature.

Full write-up: https://nosuch.biz/mailsync/

## Prerequisites

- procmail on the mail server with an existing `.procmailrc`
- [uv](https://docs.astral.sh/uv/) installed on the mail server (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A Gmail [App Password](https://myaccount.google.com/apppasswords)

## Setup

```bash
cp upload_eml.py ~/bin/
chmod +x ~/bin/upload_eml.py
~/bin/upload_eml.py --save-credentials
```

Add to `.procmailrc`:

```
PATH=$HOME/.local/bin:$PATH

:0 cw
| $HOME/bin/upload_eml.py -
```

Put it where mail you want in Gmail will reach it - typically last, so anything
your earlier recipes filed away is not also uploaded.

The two flags both matter:

- **`c`** makes this a copy. The message carries on to your normal delivery
  afterwards, so a failed upload costs the Gmail copy rather than the mail.
- **`w`** makes procmail wait for the script and check its exit code. Without
  it the exit code is discarded and a failed upload is completely silent - the
  log shows the script ran and nothing else. With it, a failure appears as
  `procmail: Program failure (1)`.

`w` means procmail waits for the IMAP round trip before continuing, which
delays delivery slightly. Gmail's IMAP latency is heavy-tailed - usually a
fraction of a second, occasionally tens of seconds - so `upload_eml.py` bounds
every socket operation at 60s to keep a stalled connection from blocking
procmail until its own `TIMEOUT` (960s by default) fires.

**Do not add an explicit delivery recipe after this one.** Mail that falls off
the end of `.procmailrc` is delivered by procmail's `$DEFAULT` fallback, so
local delivery already happens. An explicit recipe would stop processing at
that point, and any recipe below it would never run.

## Testing Upload to Gmail

```bash
# Upload a specific .eml file
upload_eml.py /path/to/message.eml

# Test with a unique Message-ID (bypasses Gmail deduplication)
upload_eml.py --fake-id /path/to/message.eml
```

## The other scripts

All are stdlib-only and deployed the same way - copy the single file.

**`unwrap_spam.py`** - extracts the original message from a SpamAssassin
wrapper, for piping from a `.procmailrc` recipe. Returns its input unchanged if
there is nothing to unwrap.

**`mailsync_housekeeping.py`** - run hourly from cron. Retries uploads that
failed (`upload_eml.py` spools them), gives up after a week by saving the
message to a `Stranded` mailbox and mailing a summary, then expires old mail
from the local INBOX. `--show-cron` checks whether cron and uv are usable and
prints the line to install; `--dry-run` reports without changing anything.

The suggested line spells out the path to `uv` rather than relying on the
shebang. Cron runs with a PATH that does not include where the uv installer
puts it, so a bare script path fails with `env: 'uv': No such file or
directory` before python starts.

**`check_deployment.py`** - deployment is a manual copy, so nothing detects
drift. This compares each file's committed, working-tree and deployed versions
and names which pair disagrees. Host and file mapping come from a config file;
`--example-config` prints a template.

It also **imports each deployed python file on the server**, because matching
digests only prove the right bytes arrived, not that they run there. If the
machine you develop on has a different python than the server, a file can import
cleanly for you and fail on the server - and a local test suite will not catch
it, since pytest imports much of the stdlib itself and masks missing submodule
imports. For a script that runs inside mail delivery, that failure is every
message failing to upload. `--no-import-check` skips it.

Exit codes are distinct because the responses differ - one is something to go
and fix, one is something to retry, one is an outage:

| code | meaning |
|---|---|
| 0 | everything matches and imports |
| 1 | some file disagrees |
| 2 | the host could not be reached, so nothing was compared |
| 3 | a deployed file is in place but does not import there |

Reporting an unreachable host as "every file is missing on the server" would be
indistinguishable from someone having deleted them all, so a transport failure
is its own verdict rather than a per-file one. The ssh call waits 10 seconds to
connect and never prompts, so an unreachable host fails quickly rather than
hanging.

## Tests

```bash
uv run --with pytest pytest -q
```

No `pyproject.toml` and no declared dependencies - pytest is supplied by uv at
run time, so the scripts stay stdlib-only and deployable as single files.
