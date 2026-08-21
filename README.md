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

## Labels: which alias the message was addressed to

If one mailbox is reached through many aliases - a different address per
correspondent - the alias that was used is worth knowing, because it names
whoever gave your address away. The forwarder records it in `X-Original-To`,
but Gmail never sees that header: it is fed by this script's `APPEND` rather
than by SMTP, so it has nothing to index and no filter of yours can key on it.

So `upload_eml.py` reads the header itself and applies a nested label at upload
time, `alias@example.test` becoming `to/example.test/alias`. Domain first, so
the sidebar groups every alias of one domain together. The label is created on
first use; there is nothing to set up.

Spam needs a second header. Where a milter tags and wraps a message at SMTP
time, the wrapping happens before the forwarder writes `X-Original-To`, so the
wrapped original never carries it - it carries `X-Envelope-To`, which the milter
writes from the SMTP envelope. Both headers are read, `X-Original-To` first.

Nothing else is read, and `Envelope-To` in particular is not. It looks like the
same fact and is not: it arrives over the wire, so it names whatever the last
hop - or the sender - chose to put there. A sender can supply an
`X-Envelope-To` too, which is why the **first** value wins; the locally written
one is prepended above the original headers.

**Only messages whose alias is not already visible get one.** If the address
appears in `To:` or `Cc:` the reading pane already shows it and a label would
be noise - on a real sample that is about seven messages in eight. What is left
is the case worth seeing: bcc'd mail, mailing lists, and spam, where `To:` says
something unrelated and the alias survives nowhere else.

Labelling happens over the same connection, straight after the append, using
the UID Gmail reports back. **It can never fail an upload.** By the time it
runs the message is already delivered, so a refusal, a stall, or anything else
unexpected costs the label and nothing more - it is not worth a
`Program failure` line for a message that arrived. Pass `--no-label` to switch
it off without redeploying.

### Addresses that live at another provider

Everything above says "the forwarder" for your own MTA, the one that writes
`X-Original-To`. This section is about a different actor one hop further out,
called **the provider** throughout: someone else's mail service, holding an
address of yours and relaying it to you.

An address at such a provider defeats all of the above. By the time the message
arrives, the envelope recipient has been rewritten to whichever of your own
addresses the relay points at, so `X-Original-To` names your endpoint and the
address you actually handed out is one hop upstream, invisible to every header
this machine writes. Labelling it truthfully would mean trusting a header that
arrived over the wire.

The way out is to encode the upstream address into the endpoint's own
localpart, so the fact travels inside the one string postfix will record:

```bash
./upload_eml.py --encode-alias someone@other.example.test example.test
someone+at+other.example.test+k+7f3qa9mx@example.test
```

Lodge that address with the provider instead of a plain one, and the message is
labelled `to/other.example.test/someone` - the address you gave out, not the
pipe it came through. Nothing needs to be configured per address and there is
no table to maintain; decoding is arithmetic.

The `+k+` part is a truncated HMAC, and it is load-bearing. These domains
usually answer on a catch-all, so anyone may send to any localpart; without a
MAC a stranger could address `security+at+elsewhere.example.test@example.test` and
plant a label under a domain they do not own. An address whose tag does not
verify decodes to nothing and falls back to the ordinary rule, which labels it
under your own domain where it is visibly yours and visibly junk.

The key lives beside the app password, in the same config file, on the machine
that runs the scripts. Generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

and add it as its own section:

```ini
[forwarding]
key = paste-the-generated-value-here
```

Until that section exists the feature is simply off: endpoints do not decode and
are labelled by their literal localpart, exactly as any other alias. Do not
re-mint the key once endpoints are lodged with a provider - every one of them
stops verifying, and mail arriving at them reverts to that literal label.

#### Adding a new provider

1. **Make sure the key exists and the scripts are deployed first.** Both are
   read at delivery time. If you change the provider before this, mail starts
   arriving at an endpoint nothing can decode and is labelled by its literal
   localpart until you catch up - untidy, not lost.

2. **Mint the address on the machine that holds the key** - the mail server, not
   your workstation, unless you have deliberately put the key on both.

   ```bash
   ~/bin/upload_eml.py --encode-alias someone@other.example.test example.test
   ```

   Run it the way cron does if a bare path fails: the uv shebang needs
   `~/.local/bin` on `PATH`, which a non-interactive shell does not have.

3. **Lodge the printed address with the provider** as its forwarding target.

4. **Let the confirmation mail be the test.** Providers generally send one to a
   new forwarding address before they start using it, and it travels the entire
   path - their relay, your MTA writing `X-Original-To`, procmail, the `APPEND`,
   the decode, the label. If it lands under `to/other.example.test/someone`, the
   whole chain works before any mail you care about depends on it.

Mail that arrived before the switch keeps its old label, so both will sit in the
sidebar for a while. That is expected, not a fault.

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

Its retry reads the same config, so it labels a recovered message exactly as the
first attempt would have - forwarding endpoints included. Deploy it alongside
`upload_eml.py` rather than on its own: if only one of the two can read the key,
the label a message ends up with depends on which attempt happened to deliver
it.

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

## Contributing

### This repo is public

The system it drives is not. Before committing any file, check it carries none
of:

- server hostnames or internal domains
- the name of any other project
- real email addresses - use `example.test`
- paths, log excerpts or details specific to one machine's setup

That applies to code, comments, docstrings, test fixtures and commit messages
alike. Operational and local context belongs outside this repo.

### Conventions

- Stdlib only, and no declared dependencies - see [Tests](#tests) for why.
- Scripts carry a `#!/usr/bin/env -S uv run --script` shebang and an empty
  `dependencies = []` block.
- Keep a pure, testable core separate from the thin `__main__` that wraps it.

### Care

These scripts run inside a mail delivery path, where an unhandled exception
costs a copy of someone's mail. Prefer failing loudly over failing silently,
and prefer returning the input unchanged over raising.

A measured maximum is not a bound. A 60 s timeout chosen as "4x the worst
observed" was exceeded by real traffic within the hour.

Note that the tests talk to a **live** mailbox, so running them has consequences
the suite itself does not: send APPEND tests to `"[Gmail]/Trash"` rather than
INBOX, and avoid bursts of IMAP connections.
