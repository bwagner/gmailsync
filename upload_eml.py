#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import configparser
import email

# Neither submodule is implied by `import email`. Both are named below - one in
# an annotation - and the server's python 3.12 evaluates annotations eagerly, so
# omitting these makes the module fail to import there while working on a 3.14
# mac, where PEP 649 defers them.
import email.message
import email.utils
import imaplib
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import NamedTuple

CONFIG_PATH = Path.home() / ".config" / "mailsync" / "config"

# Messages whose upload failed are kept here for a later retry pass. Under
# XDG state rather than config: this is data the program manages, not settings.
PENDING_DIR = Path.home() / ".local" / "state" / "mailsync" / "pending"
SPOOL_SUFFIX = ".eml"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Bounds every blocking socket operation - connect, and each read and write -
# not the session as a whole. Without it a hung server blocks forever, which is
# survivable only while the caller fires and forgets; a caller that waits for
# this process waits with it.
IMAP_TIMEOUT_SECONDS = 60

# IMAP4 tagged-response status meaning the server accepted the command (RFC 3501).
STATUS_OK = "OK"

# RFC 4315: the tagged OK of an APPEND may carry [APPENDUID <uidvalidity> <uid>],
# naming the message that was just stored. Only a single uid is accepted here -
# the RFC also permits a uid-set for MULTIAPPEND, which this program never
# issues, so a set means an assumption broke and nothing should be returned.
APPENDUID_PATTERN = re.compile(r"\[APPENDUID\s+(\d+)\s+(\d+)\s*\]", re.IGNORECASE)


class AppendUid(NamedTuple):
    """Where a just-appended message landed, so it can be acted on directly."""

    uidvalidity: int
    uid: int


# --- which alias this message was addressed to ------------------------------
#
# One mailbox is reached through many aliases, one per correspondent, and which
# was used is the useful fact: it names whoever gave the address away. Postfix
# records it in X-Original-To, and because gmail is fed by our own APPEND rather
# than by SMTP it never sees that header - so the label has to be computed here.

HEADER_X_ORIGINAL_TO = "X-Original-To"
HEADER_ENVELOPE_TO = "Envelope-To"
HEADER_TO = "To"
HEADER_CC = "Cc"

# Where the alias may be read from, in order. Deliberately short: Delivered-To names the final hop - the real mailbox, not
# the alias - and To: only carries the alias in the cases this skips anyway.
ALIAS_HEADER_PRECEDENCE = (HEADER_X_ORIGINAL_TO, HEADER_ENVELOPE_TO)

# Headers a human sees in the reading pane. An alias visible in one of these
# needs no label - the message already identifies itself.
VISIBLE_RECIPIENT_HEADERS = (HEADER_TO, HEADER_CC)

LABEL_ROOT = "to"
LABEL_SEPARATOR = "/"
# Gmail reads the separator as a nesting level, so it cannot survive inside a
# single component of a label.
LABEL_SEPARATOR_REPLACEMENT = "_"
AT_SIGN = "@"


def alias_from_headers(msg: email.message.Message) -> str | None:
    """The bare address this message was actually delivered to, lowercased.

    Lowercased because addresses differing only in case would otherwise split
    into two gmail labels for one alias.
    """
    for header in ALIAS_HEADER_PRECEDENCE:
        for raw in msg.get_all(header) or []:
            _, address = email.utils.parseaddr(str(raw))
            if address and AT_SIGN in address:
                return address.lower()
    return None


def label_for_alias(alias: str | None) -> str | None:
    """Map an address to its nested gmail label:
    'alias@example.test' -> 'to/example.test/alias'.

    Domain first, so gmail's sidebar groups every alias of one domain together.
    Returns None rather than a catch-all label when there is nothing to map:
    every message uploaded here came through procmail and has X-Original-To, so
    a catch-all would collect noise rather than anything worth looking at.
    """
    if not alias or AT_SIGN not in alias:
        return None
    localpart, _, domain = alias.rpartition(AT_SIGN)
    localpart = localpart.replace(LABEL_SEPARATOR, LABEL_SEPARATOR_REPLACEMENT)
    domain = domain.replace(LABEL_SEPARATOR, LABEL_SEPARATOR_REPLACEMENT)
    if not localpart or not domain:
        return None
    return LABEL_SEPARATOR.join((LABEL_ROOT, domain, localpart))


def alias_is_visible(msg: email.message.Message, alias: str | None) -> bool:
    """Can the alias already be read off the message's visible recipients?

    Checks To: and Cc:, since a message may name the alias alongside several
    other recipients and still be perfectly legible.
    """
    if not alias:
        return False
    values = [v for header in VISIBLE_RECIPIENT_HEADERS for v in msg.get_all(header) or []]
    return any(address.lower() == alias.lower() for _, address in email.utils.getaddresses(values))


def label_for_message(msg: email.message.Message) -> str | None:
    """The label to apply to this message, or None to leave it unlabelled.

    Only worth writing when the alias cannot already be read off To:/Cc:.
    Otherwise it duplicates what the reading pane shows and buries the messages
    that are actually opaque - on a live sample only 4 of 26 were.
    """
    alias = alias_from_headers(msg)
    if alias_is_visible(msg, alias):
        return None
    return label_for_alias(alias)


class AppendError(Exception):
    """The IMAP server refused the APPEND."""


def load_config() -> tuple[str, str]:
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    try:
        return config["gmail"]["user"], config["gmail"]["app_password"]
    except KeyError:
        return "", ""


def save_config(user: str, app_password: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config["gmail"] = {"user": user, "app_password": app_password}
    with CONFIG_PATH.open("w") as f:
        config.write(f)
    CONFIG_PATH.chmod(0o600)
    print(f"Credentials saved to {CONFIG_PATH}")


def _describe(data: list | None) -> str:
    """Flatten imaplib's response data into one readable line."""
    parts = []
    for item in data or []:
        if item is None:
            continue
        parts.append(item.decode(errors="replace") if isinstance(item, bytes) else str(item))
    return " ".join(parts)


def parse_appenduid(data: list | None) -> AppendUid | None:
    """The UID gmail assigned to a just-appended message, if it said.

    Gmail returns APPENDUID **without advertising UIDPLUS**, so this is observed
    behaviour rather than a contract: every failure to find or parse it returns
    None. The caller's fallback is to search for the message by Message-ID; a
    missing UID is not an upload failure and must never be treated as one.
    """
    match = APPENDUID_PATTERN.search(_describe(data))
    if match is None:
        return None
    return AppendUid(int(match.group(1)), int(match.group(2)))


def check_append_result(typ: str, data: list | None) -> None:
    """Raise AppendError unless the server accepted the APPEND.

    imaplib raises on a tagged BAD by itself, but returns NO as an ordinary
    result - so an unchecked append() reports quota, size and policy refusals
    as success.
    """
    if typ == STATUS_OK:
        return
    raise AppendError(f"APPEND failed: {typ} {_describe(data)}".rstrip())


def read_eml(eml_path: str) -> bytes:
    """Read the message from a file, or from stdin when eml_path is '-'."""
    return sys.stdin.buffer.read() if eml_path == "-" else Path(eml_path).read_bytes()


def patch_message_id(eml: bytes) -> tuple[bytes, str]:
    """Replace the Message-ID so gmail treats this as a new message.

    Gmail dedupes on Message-ID, which is normally what you want - it makes a
    retry idempotent - so this exists only for deliberately forcing a copy.
    A message with no Message-ID is returned unchanged.
    """
    new_id = f"<test-{uuid.uuid4()}@mailsync.local>"
    return re.sub(rb"(?im)^Message-ID:.*$", f"Message-ID: {new_id}".encode(), eml, count=1), new_id


def spool_failed(eml: bytes, pending_dir: Path = PENDING_DIR) -> Path:
    """Keep a message that could not be uploaded, so a later pass can retry it.

    Written under a dot-prefixed temporary name and renamed into place, so a
    retry running concurrently never reads a half-written message.
    """
    pending_dir = Path(pending_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time():.6f}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    tmp = pending_dir / f".{stamp}.partial"
    tmp.write_bytes(eml)
    final = pending_dir / f"{stamp}{SPOOL_SUFFIX}"
    tmp.rename(final)
    return final


def upload_eml_to_gmail(eml: bytes, gmail_user: str, app_password: str, mailbox: str = "INBOX", imap_factory=imaplib.IMAP4_SSL, timeout: int = IMAP_TIMEOUT_SECONDS) -> AppendUid | None:
    """Append the message to gmail, returning where it landed if gmail said."""
    msg = email.message_from_bytes(eml)
    date_str = msg.get("Date")
    if date_str:
        timestamp = email.utils.parsedate_to_datetime(date_str).timestamp()
    else:
        timestamp = time.time()

    with imap_factory(IMAP_HOST, IMAP_PORT, timeout=timeout) as imap:
        imap.login(gmail_user, app_password)
        typ, data = imap.append(mailbox, None, imaplib.Time2Internaldate(timestamp), eml)
        print(f"Result: {typ} {_describe(data)}".rstrip())
        check_append_result(typ, data)
        return parse_appenduid(data)


if __name__ == "__main__":
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Upload an .eml file to Gmail via IMAP")
    parser.add_argument("eml", nargs="?", help="Path to the .eml file")
    parser.add_argument("--user", help="Gmail address (overrides config)")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--fake-id", action="store_true", help="Replace Message-ID with a unique value to force a new copy")
    parser.add_argument("--save-credentials", action="store_true", help="Prompt for credentials and save to config file")
    parser.add_argument("--no-spool", "-S", dest="spool", action="store_false", help=f"Do not keep a failed message in {PENDING_DIR} for retry")
    args = parser.parse_args()

    if args.save_credentials:
        user = args.user or input("Gmail address: ")
        app_password = getpass.getpass("Gmail App Password: ")
        save_config(user, app_password)
        if not args.eml:
            sys.exit(0)

    saved_user, saved_password = load_config()
    gmail_user = args.user or saved_user
    if not gmail_user:
        gmail_user = input("Gmail address: ")

    app_password = saved_password or getpass.getpass("Gmail App Password: ")

    eml = read_eml(args.eml or "-")
    if args.fake_id:
        eml, patched_id = patch_message_id(eml)
        print(f"Patched Message-ID: {patched_id}")

    try:
        upload_eml_to_gmail(eml, gmail_user, app_password, args.mailbox)
    except (AppendError, TimeoutError) as exc:
        reason = exc if isinstance(exc, AppendError) else f"IMAP timed out after {IMAP_TIMEOUT_SECONDS}s: {exc}"
        print(reason, file=sys.stderr)
        # Keep the message so a retry pass can upload it later. Without this the
        # only copy is whatever the caller happens to have kept.
        if args.spool:
            try:
                print(f"Spooled for retry: {spool_failed(eml)}", file=sys.stderr)
            except OSError as spool_error:
                print(f"Could not spool the message: {spool_error}", file=sys.stderr)
        sys.exit(1)
