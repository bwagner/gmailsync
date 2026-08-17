#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import base64
import configparser
import email
import hashlib
import hmac

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
HEADER_X_ENVELOPE_TO = "X-Envelope-To"
HEADER_TO = "To"
HEADER_CC = "Cc"

# Where the alias may be read from, in order. Both are written by this machine:
# X-Original-To by postfix at local delivery, X-Envelope-To by the spam milter at
# SMTP time. The second is not redundant - the milter wraps a spam message before
# postfix writes the first, so the unwrapped original carries only X-Envelope-To.
#
# Deliberately short. Delivered-To names the final hop - the real mailbox, not
# the alias - and To: only carries the alias in the cases this skips anyway.
# Envelope-To is excluded for a sharper reason: unlike these two it arrives over
# the wire, so it names whatever the last forwarder or the sender chose to put
# there. A sender may also supply an X-Envelope-To, and it is not stripped; the
# milter prepends its own above the original headers, so reading the first value
# is what keeps a forged one from choosing the label.
ALIAS_HEADER_PRECEDENCE = (HEADER_X_ORIGINAL_TO, HEADER_X_ENVELOPE_TO)

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


# --- an alias that carries a third party's address inside it ----------------
#
# Mail sent to an address at another provider, which forwards it here, arrives
# with our own endpoint in X-Original-To: the address actually handed out is one
# hop upstream and no header this machine writes can see it. Encoding it into
# the endpoint's own localpart puts it back where postfix will record it:
#
#     someone@other.example.test  ->  someone+at+other.example.test+k+7f3qa9mx@example.test
#
# The MAC is not decoration. These domains answer on a catch-all, so anyone may
# send to any localpart; without it, a stranger could address
# security+at+elsewhere.test@example.test and plant a label under a domain they do
# not own. With it, an unverified address decodes to nothing and falls through
# to the ordinary rule, which labels it under our own domain where it is
# visibly ours and visibly junk.

FORWARD_AT_SEPARATOR = "+at+"
FORWARD_KEY_SEPARATOR = "+k+"

# base32 of an HMAC-SHA256, truncated. The alphabet is a-z2-7 once lowercased,
# every character of which is legal in a localpart and survives a hop that
# case-folds one. 8 characters is 40 bits - far past brute force over SMTP,
# and the truncation leaks nothing about the key.
FORWARD_MAC_LENGTH = 8

# RFC 5321 caps a localpart at 64 octets. Refusing to mint a longer one is the
# only chance to find out before some hop rejects mail at delivery time.
MAX_LOCALPART_OCTETS = 64

# A carried domain must look like one. Guards the label tree specifically: the
# decoded domain becomes the top level of the label, so a component with no dot
# has no business there however well it verifies.
FORWARD_DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

CONFIG_SECTION_FORWARDING = "forwarding"
CONFIG_OPTION_FORWARD_KEY = "key"


def forward_mac(upstream: str, key: str) -> str:
    """The tag proving this machine minted an endpoint for `upstream`."""
    digest = hmac.new(key.encode(), upstream.strip().lower().encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:FORWARD_MAC_LENGTH]


def encode_forwarded_alias(upstream: str, key: str, local_domain: str) -> str:
    """The endpoint address to give a third-party forwarder, carrying `upstream`.

    Raises ValueError rather than minting an address that cannot work: this runs
    when a forward is being set up, where a refusal costs a moment, and the
    alternative is discovering it when mail is already being lost.
    """
    localpart, _, domain = (upstream or "").strip().lower().rpartition(AT_SIGN)
    if not localpart or not FORWARD_DOMAIN_PATTERN.match(domain):
        raise ValueError(f"not an address that can be carried: {upstream!r}")
    carried = f"{localpart}{AT_SIGN}{domain}"
    encoded = f"{localpart}{FORWARD_AT_SEPARATOR}{domain}{FORWARD_KEY_SEPARATOR}{forward_mac(carried, key)}"
    if len(encoded.encode()) > MAX_LOCALPART_OCTETS:
        raise ValueError(f"localpart would be {len(encoded.encode())} octets, over the {MAX_LOCALPART_OCTETS} RFC 5321 allows: {carried!r}")
    return f"{encoded}{AT_SIGN}{local_domain}"


def decode_forwarded_alias(alias: str | None, key: str | None) -> str | None:
    """The upstream address an endpoint carries, or None if it carries none.

    None is the answer for every kind of no - not an endpoint, no key
    configured, a MAC that does not verify - because the caller's response to
    all of them is the same: fall through to the ordinary rule. Both splits take
    the *last* occurrence, so an address containing a separator still survives
    the round trip.
    """
    if not alias or not key:
        return None
    localpart, _, _local_domain = alias.strip().lower().rpartition(AT_SIGN)
    if not localpart:
        return None
    carried, separator, mac = localpart.rpartition(FORWARD_KEY_SEPARATOR)
    if not separator:
        return None
    upstream_local, separator, upstream_domain = carried.rpartition(FORWARD_AT_SEPARATOR)
    if not separator or not upstream_local or not FORWARD_DOMAIN_PATTERN.match(upstream_domain):
        return None
    upstream = f"{upstream_local}{AT_SIGN}{upstream_domain}"
    if not hmac.compare_digest(mac, forward_mac(upstream, key)):
        return None
    return upstream


def label_for_message(msg: email.message.Message, forward_key: str | None = None) -> str | None:
    """The label to apply to this message, or None to leave it unlabelled.

    Only worth writing when the alias cannot already be read off To:/Cc:.
    Otherwise it duplicates what the reading pane shows and buries the messages
    that are actually opaque - on a live sample only 4 of 26 were.

    Where the alias is a forwarding endpoint, the address it carries replaces it
    outright, and is what the visibility test then asks about: the endpoint is
    plumbing, and seeing it in To: tells a reader nothing about who was written
    to. The key is optional, so the feature is simply off until one exists.
    """
    alias = alias_from_headers(msg)
    alias = decode_forwarded_alias(alias, forward_key) or alias
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


def load_forward_key() -> str | None:
    """The HMAC key that makes a forwarding endpoint verifiable, if one is set.

    Absent is a normal state, not an error: without it every alias is labelled
    literally, exactly as before the encoding existed.
    """
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    key = config.get(CONFIG_SECTION_FORWARDING, CONFIG_OPTION_FORWARD_KEY, fallback="").strip()
    return key or None


def save_config(user: str, app_password: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    # Read before writing. This file also holds the forwarding key, and a fresh
    # parser would drop it silently - re-minting it would invalidate every
    # endpoint address already lodged with a forwarder.
    config.read(CONFIG_PATH)
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


# --- writing the label onto the message that was just appended --------------
#
# Everything below is subordinate to one rule: by the time any of it runs the
# message is already in gmail, so no failure here may be reported as an upload
# failure. A missing label is a cosmetic loss; a reported failure would spool
# and re-upload a message that has already arrived, and would put a
# `Program failure` line in the procmail log for a message that was delivered.

HEADER_MESSAGE_ID = "Message-ID"

# STORE is only valid in the selected state, so the mailbox has to be selected
# even though APPEND did not need it.
STORE_COMMAND = "STORE"
SEARCH_COMMAND = "SEARCH"

# Gmail's own label attribute (X-GM-EXT-1). Adding a custom label this way
# creates it on first use, and `/` nests it in the sidebar.
ADD_LABELS_ITEM = "+X-GM-LABELS"

# Gmail's native search syntax over IMAP, and the operator naming one message.
GMAIL_RAW_SEARCH = "X-GM-RAW"
RFC822MSGID_OPERATOR = "rfc822msgid:"

# The untagged response SELECT reports the mailbox's UIDVALIDITY in.
UIDVALIDITY_CODE = "UIDVALIDITY"

# RFC 3501 quoted strings: only these two characters need escaping, and the
# backslash must be escaped first or it would escape the escapes.
IMAP_QUOTE = '"'
IMAP_ESCAPE = "\\"

MESSAGE_ID_BRACKETS = "<>"

# A Message-ID identifies exactly one message, and gmail dedupes on it, so more
# than one hit means the assumption broke and nothing should be labelled.
EXPECTED_SEARCH_HITS = 1


def imap_quote(value: str) -> str:
    """Wrap a value as an IMAP quoted string (RFC 3501).

    Without this an embedded double quote reads as the end of the string and
    gmail answers BAD rather than doing anything.
    """
    escaped = value.replace(IMAP_ESCAPE, IMAP_ESCAPE * 2).replace(IMAP_QUOTE, IMAP_ESCAPE + IMAP_QUOTE)
    return f"{IMAP_QUOTE}{escaped}{IMAP_QUOTE}"


def label_store_argument(label: str) -> str:
    """The parenthesised label list a STORE +X-GM-LABELS takes."""
    return f"({imap_quote(label)})"


def rfc822msgid_query(message_id: str | None) -> str | None:
    """A gmail search naming exactly this message, or None if it cannot be built.

    The angle brackets are RFC 5322 syntax; gmail's operator wants the bare id.
    Non-ASCII yields None rather than a query: imaplib encodes the command line
    as ASCII, so sending one would raise inside the delivery path, and no label
    is a better outcome than that.
    """
    if not message_id:
        return None
    bare = message_id.strip().strip(MESSAGE_ID_BRACKETS).strip()
    if not bare or not bare.isascii():
        return None
    return RFC822MSGID_OPERATOR + bare


def parse_search_uids(data: list | None) -> list[int]:
    """The UIDs in a UID SEARCH response, ignoring anything unparseable."""
    uids = []
    for token in _describe(data).split():
        try:
            uids.append(int(token))
        except ValueError:
            continue
    return uids


def selected_uidvalidity(imap) -> int | None:
    """UIDVALIDITY of the mailbox imaplib has selected, if the server said."""
    _, data = imap.response(UIDVALIDITY_CODE)
    values = parse_search_uids(data)
    return values[0] if values else None


def resolve_uid(imap, appenduid: AppendUid | None, message_id: str | None) -> int | None:
    """Which UID in the selected mailbox holds the message just appended.

    Prefers what APPEND reported, but only once the selected mailbox agrees
    about UIDVALIDITY - a UID is meaningless without it, and this account really
    does hand out a different one per mailbox. On disagreement, or when gmail
    said nothing, fall back to finding the message by its Message-ID.
    """
    if appenduid is not None:
        reported = selected_uidvalidity(imap)
        if reported is None or reported == appenduid.uidvalidity:
            return appenduid.uid
    query = rfc822msgid_query(message_id)
    if query is None:
        return None
    typ, data = imap.uid(SEARCH_COMMAND, None, GMAIL_RAW_SEARCH, imap_quote(query))
    if typ != STATUS_OK:
        return None
    uids = parse_search_uids(data)
    return uids[0] if len(uids) == EXPECTED_SEARCH_HITS else None


def apply_label(imap, mailbox: str, label: str, appenduid: AppendUid | None, message_id: str | None) -> bool:
    """Add the gmail label to the message just appended. True if it was applied.

    Reports rather than raises: the caller has already delivered the message.
    """
    typ, _ = imap.select(mailbox)
    if typ != STATUS_OK:
        return False
    uid = resolve_uid(imap, appenduid, message_id)
    if uid is None:
        return False
    typ, _ = imap.uid(STORE_COMMAND, str(uid), ADD_LABELS_ITEM, label_store_argument(label))
    return typ == STATUS_OK


class UploadResult(NamedTuple):
    """What became of one message: where it landed, and whether it got labelled.

    `append_response` carries the server's own words about the APPEND, so the
    caller can report them without this function writing to stdout itself.
    """

    appenduid: AppendUid | None
    label: str | None
    labelled: bool
    append_response: str


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


def upload_eml_to_gmail(eml: bytes, gmail_user: str, app_password: str, mailbox: str = "INBOX", imap_factory=imaplib.IMAP4_SSL, timeout: int = IMAP_TIMEOUT_SECONDS, apply_labels: bool = True, forward_key: str | None = None) -> UploadResult:
    """Append the message to gmail, and label it with the alias it was sent to.

    The label is applied over the same connection, and only when the alias is
    not already visible in To:/Cc: - which is most messages, and those pay no
    extra round trip at all.

    **Nothing here prints to stdout.** Two callers drive this with opposite
    conventions - procmail discards stdout, cron mails every byte of it - so a
    success line written here reaches the mailbox as an alarm about a message
    that arrived perfectly well. Success travels back in UploadResult and the
    caller decides. Warnings stay on stderr: only stderr reaches procmail's log,
    and cron mail is the only place the retry path can report a lost label.
    """
    msg = email.message_from_bytes(eml)
    date_str = msg.get("Date")
    if date_str:
        timestamp = email.utils.parsedate_to_datetime(date_str).timestamp()
    else:
        timestamp = time.time()

    with imap_factory(IMAP_HOST, IMAP_PORT, timeout=timeout) as imap:
        imap.login(gmail_user, app_password)
        typ, data = imap.append(mailbox, None, imaplib.Time2Internaldate(timestamp), eml)
        check_append_result(typ, data)
        append_response = f"{typ} {_describe(data)}".rstrip()
        appenduid = parse_appenduid(data)

        label = label_for_message(msg, forward_key) if apply_labels else None
        if label is None:
            return UploadResult(appenduid, None, False, append_response)

        # The message is in gmail from here on, so nothing below may raise.
        # Anything at all - a refusal, a stall, a response shaped differently
        # than expected - costs the label and leaves the delivery successful.
        try:
            labelled = apply_label(imap, mailbox, label, appenduid, msg.get(HEADER_MESSAGE_ID))
        except Exception as exc:  # noqa: BLE001
            print(f"Uploaded but could not label {label}: {exc}", file=sys.stderr)
            return UploadResult(appenduid, label, False, append_response)
        if not labelled:
            print(f"Uploaded but could not label {label}", file=sys.stderr)
        return UploadResult(appenduid, label, labelled, append_response)


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
    parser.add_argument("--no-label", "-L", dest="label", action="store_false", help="Do not label the message with the alias it was addressed to")
    parser.add_argument("--encode-alias", "-e", nargs=2, metavar=("UPSTREAM", "DOMAIN"), help="Print the endpoint address to lodge with a forwarder, carrying UPSTREAM, at DOMAIN")
    args = parser.parse_args()

    if args.encode_alias:
        forward_key = load_forward_key()
        if not forward_key:
            print(f"No [{CONFIG_SECTION_FORWARDING}] {CONFIG_OPTION_FORWARD_KEY} in {CONFIG_PATH}", file=sys.stderr)
            sys.exit(1)
        try:
            print(encode_forwarded_alias(args.encode_alias[0], forward_key, args.encode_alias[1]))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

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
        result = upload_eml_to_gmail(eml, gmail_user, app_password, args.mailbox, apply_labels=args.label, forward_key=load_forward_key())
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

    # Only here, at the entry point procmail drives. The library stays silent so
    # that the same call from the hourly cron job does not mail a success.
    print(f"Result: {result.append_response}")
    if result.labelled:
        print(f"Labelled: {result.label}")
