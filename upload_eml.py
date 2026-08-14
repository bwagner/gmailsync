#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import configparser
import email
import imaplib
import re
import sys
import time
import uuid
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "mailsync" / "config"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Bounds every blocking socket operation - connect, and each read and write -
# not the session as a whole. Without it a hung server blocks forever, which is
# survivable only while the caller fires and forgets; a caller that waits for
# this process waits with it.
IMAP_TIMEOUT_SECONDS = 60

# IMAP4 tagged-response status meaning the server accepted the command (RFC 3501).
STATUS_OK = "OK"


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


def check_append_result(typ: str, data: list | None) -> None:
    """Raise AppendError unless the server accepted the APPEND.

    imaplib raises on a tagged BAD by itself, but returns NO as an ordinary
    result - so an unchecked append() reports quota, size and policy refusals
    as success.
    """
    if typ == STATUS_OK:
        return
    raise AppendError(f"APPEND failed: {typ} {_describe(data)}".rstrip())


def upload_eml_to_gmail(eml_path: str, gmail_user: str, app_password: str, mailbox: str = "INBOX", fake_id: bool = False, imap_factory=imaplib.IMAP4_SSL, timeout: int = IMAP_TIMEOUT_SECONDS) -> None:
    eml = sys.stdin.buffer.read() if eml_path == "-" else Path(eml_path).read_bytes()

    if fake_id:
        new_id = f"<test-{uuid.uuid4()}@mailsync.local>"
        eml = re.sub(rb"(?im)^Message-ID:.*$", f"Message-ID: {new_id}".encode(), eml, count=1)
        print(f"Patched Message-ID: {new_id}")

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


if __name__ == "__main__":
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Upload an .eml file to Gmail via IMAP")
    parser.add_argument("eml", nargs="?", help="Path to the .eml file")
    parser.add_argument("--user", help="Gmail address (overrides config)")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--fake-id", action="store_true", help="Replace Message-ID with a unique value to force a new copy")
    parser.add_argument("--save-credentials", action="store_true", help="Prompt for credentials and save to config file")
    args = parser.parse_args()

    if args.save_credentials:
        user = args.user or input("Gmail address: ")
        app_password = getpass.getpass("Gmail App Password: ")
        save_config(user, app_password)
        if not args.eml:
            exit(0)

    saved_user, saved_password = load_config()
    gmail_user = args.user or saved_user
    if not gmail_user:
        gmail_user = input("Gmail address: ")

    app_password = saved_password or getpass.getpass("Gmail App Password: ")

    eml_path = args.eml or "-"
    try:
        upload_eml_to_gmail(eml_path, gmail_user, app_password, args.mailbox, args.fake_id)
    except AppendError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except TimeoutError as exc:
        print(f"IMAP timed out after {IMAP_TIMEOUT_SECONDS}s: {exc}", file=sys.stderr)
        sys.exit(1)
