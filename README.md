# gmailsync

Uploads incoming mail to Gmail via IMAP, triggered by procmail. A replacement for Gmail's discontinued ["fetch mail from other accounts"](https://support.google.com/mail/answer/16604719) feature.

Full write-up: https://nosuch.biz/docs/mailsync/

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

:0 c
| $HOME/bin/upload_eml.py -

:0
$HOME/Maildir/
```

## Usage

```bash
# Upload a specific .eml file
upload_eml.py /path/to/message.eml

# Test with a unique Message-ID (bypasses Gmail deduplication)
upload_eml.py --fake-id /path/to/message.eml
```
