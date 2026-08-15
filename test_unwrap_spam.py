"""Tests for unwrap_spam.

Run with:   uv run --with pytest pytest -q

Same arrangement as test_upload_eml.py: no pyproject.toml, no installed
dependencies, pytest supplied at run time by uv - unwrap_spam.py is stdlib-only
and deployed by copying a single file.

These tests characterise the behaviour of a script that is already in service,
so they record what it does rather than specify what it ought to do. Where the
current behaviour is questionable it is called out in the test's docstring.
"""

import email

import pytest

from unwrap_spam import unwrap_spam

BOUNDARY = b"----------=_BOUNDARY"

ORIGINAL_SUBJECT = "Cheap pills"
ORIGINAL_MESSAGE_ID = "<orig-1@example.test>"
ORIGINAL_BODY = "Buy now."
WRAPPER_SUBJECT = "*****SPAM***** Cheap pills"
SPAM_FLAG_HEADER = "X-Spam-Flag"


def wrap(inner: bytes) -> bytes:
    """Build a SpamAssassin report_safe=1 wrapper around `inner`.

    That is the shape this script exists to undo: multipart/mixed, a text/plain
    report, then the untouched original as message/rfc822.
    """
    return (
        b"From: spammer@example.test\r\n"
        b"To: alias@example.test\r\n"
        b"Subject: " + WRAPPER_SUBJECT.encode() + b"\r\n"
        b"X-Spam-Flag: YES\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="' + BOUNDARY + b'"\r\n'
        b"\r\n"
        b"This is a multi-part message in MIME format.\r\n"
        b"\r\n"
        b"--" + BOUNDARY + b"\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"\r\n"
        b"Spam detection results: score=12.3\r\n"
        b"\r\n"
        b"--" + BOUNDARY + b"\r\n"
        b"Content-Type: message/rfc822; x-spam-type=original\r\n"
        b"Content-Description: original message before SpamAssassin\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + inner + b"\r\n"
        b"--" + BOUNDARY + b"--\r\n"
    )


ORIGINAL = (
    b"From: spammer@example.test\r\n"
    b"To: alias@example.test\r\n"
    b"Subject: " + ORIGINAL_SUBJECT.encode() + b"\r\n"
    b"Message-ID: " + ORIGINAL_MESSAGE_ID.encode() + b"\r\n"
    b"\r\n" + ORIGINAL_BODY.encode() + b"\r\n"
)

WRAPPED = wrap(ORIGINAL)

PLAIN = b"From: friend@example.test\r\nSubject: lunch?\r\n\r\nsee you at one\r\n"


def unwrapped():
    return email.message_from_bytes(unwrap_spam(WRAPPED))


# --- the happy path ---------------------------------------------------------


def test_returns_the_inner_message_not_the_wrapper():
    assert unwrapped().get("Subject") == ORIGINAL_SUBJECT


def test_drops_the_spam_wrapper_headers():
    assert unwrapped().get(SPAM_FLAG_HEADER) is None


def test_preserves_the_original_message_id():
    """Gmail dedupes on Message-ID and it is the only stable handle for finding
    the message again later, so losing it here would be expensive."""
    assert unwrapped().get("Message-ID") == ORIGINAL_MESSAGE_ID


def test_preserves_the_body():
    assert ORIGINAL_BODY in unwrapped().get_payload()


def test_output_reparses_as_an_email():
    """The output is piped onward and eventually APPENDed, so it has to survive
    a round trip through the parser that consumes it."""
    reparsed = email.message_from_bytes(unwrap_spam(WRAPPED))
    assert reparsed.get("From") == "spammer@example.test"
    assert not reparsed.defects


def test_eight_bit_body_survives():
    umlauts = "cäfé"
    out = unwrap_spam(wrap(f"Subject: äö\r\n\r\n{umlauts}\r\n".encode()))
    assert umlauts.encode() in out


# --- the fallback: anything that is not a wrapper comes back untouched ------


def test_plain_message_is_returned_unchanged():
    assert unwrap_spam(PLAIN) == PLAIN


def test_empty_input_is_returned_unchanged():
    assert unwrap_spam(b"") == b""


def test_input_that_is_not_email_at_all_is_returned_unchanged():
    garbage = b"\x00\xff not an email"
    assert unwrap_spam(garbage) == garbage


def test_a_headerless_inner_message_falls_back_to_the_input():
    """Subtle: Message.__len__ is the header count, so an inner message with no
    headers is falsy and the `if inner:` guard rejects it. A body-only
    message/rfc822 part therefore yields the wrapper, not the body."""
    wrapped = wrap(b"just a body, no headers\r\n")
    assert unwrap_spam(wrapped) == wrapped


# --- documented quirks ------------------------------------------------------


def test_the_first_rfc822_part_wins():
    """walk() is depth-first pre-order and the function returns on the first
    match, so an outer part shadows any nested one."""
    two_parts = (
        b"Subject: x\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="' + BOUNDARY + b'"\r\n\r\n'
        b"--" + BOUNDARY + b"\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Subject: first\r\n\r\nA\r\n"
        b"\r\n--" + BOUNDARY + b"\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Subject: second\r\n\r\nB\r\n"
        b"\r\n--" + BOUNDARY + b"--\r\n"
    )
    assert email.message_from_bytes(unwrap_spam(two_parts)).get("Subject") == "first"


def test_output_line_endings_become_lf():
    """as_bytes() re-serialises with LF regardless of the input's line endings.

    This pins the round trip; it is not the origin of LF-terminated mail. Mail
    handed to a Unix MDA is already LF-terminated, so in practice the function
    turns LF into LF and every path into the uploader looks the same, unwrapped
    or not. RFC 3501 asks for CRLF in an APPEND literal, but normalising for
    that belongs in the uploader, where every message passes - not here, where
    the output is also written to a local maildir that wants LF.
    """
    out = unwrap_spam(WRAPPED)
    assert b"\r\n" not in out
    assert b"\n" in out


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\xff",
        PLAIN,
        wrap(b""),
        wrap(b"just a body\r\n"),
        wrap(b"From: a@example.test\r\n"),
        WRAPPED,
        (b'Subject: x\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
         b"Content-Type: message/rfc822\r\n\r\nFrom: a@example.test\r\n\r\nbody\r\n"),
    ],
    ids=[
        "empty",
        "not-email",
        "plain",
        "empty-rfc822-part",
        "headerless-inner",
        "headers-only-inner",
        "well-formed-wrapper",
        "truncated-multipart",
    ],
)
def test_never_raises(data):
    """It runs inside a mail delivery path, where a traceback costs a copy of
    the mail. No input may make it throw."""
    unwrap_spam(data)
