#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Unwrap a SpamAssassin-tagged email: extract the original message from the
embedded message/rfc822 MIME part and write it to stdout.
Falls back to the original input unchanged if no such part is found.

Usage (via procmail):
    :0 fw
    | ~/bin/unwrap_spam.py
"""

import email
import sys


def unwrap_spam(data: bytes) -> bytes:
    msg = email.message_from_bytes(data)
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0)
            if inner:
                return inner.as_bytes()
    return data


if __name__ == "__main__":
    sys.stdout.buffer.write(unwrap_spam(sys.stdin.buffer.read()))
