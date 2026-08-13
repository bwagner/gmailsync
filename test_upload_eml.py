"""Tests for upload_eml.

Run with:   uv run --with pytest pytest -q

The repo deliberately has no pyproject.toml and no installed dependencies -
upload_eml.py is stdlib-only and deployed by copying a single file - so pytest
is supplied at run time by uv rather than declared as a project dependency.
"""

import pytest

import upload_eml

# IMAP4 tagged-response statuses (RFC 3501). imaplib raises on BAD by itself,
# so NO is the status that would otherwise pass silently.
STATUS_OK = "OK"
STATUS_NO = "NO"


def test_append_error_is_an_exception():
    assert issubclass(upload_eml.AppendError, Exception)


def test_ok_status_does_not_raise():
    upload_eml.check_append_result(STATUS_OK, [b"[APPENDUID 1 2] (Success)"])


def test_no_status_raises_append_error():
    with pytest.raises(upload_eml.AppendError):
        upload_eml.check_append_result(STATUS_NO, [b"[OVERQUOTA] Not enough storage space"])


def test_error_message_carries_the_status():
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload_eml.check_append_result(STATUS_NO, [b"[OVERQUOTA] Not enough storage space"])
    assert STATUS_NO in str(excinfo.value)


def test_error_message_carries_the_server_text():
    """The procmail log is the only place this surfaces, so it must be readable."""
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload_eml.check_append_result(STATUS_NO, [b"[OVERQUOTA] Not enough storage space"])
    assert "Not enough storage space" in str(excinfo.value)


def test_bytes_in_data_are_decoded_not_repred():
    """b'...' leaking into the log would be noise; decode it."""
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload_eml.check_append_result(STATUS_NO, [b"refused"])
    assert "b'refused'" not in str(excinfo.value)


def test_str_data_is_accepted_too():
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload_eml.check_append_result(STATUS_NO, ["refused"])
    assert "refused" in str(excinfo.value)


def test_none_in_data_does_not_crash():
    """imaplib yields [None] when a command produced no untagged response."""
    with pytest.raises(upload_eml.AppendError):
        upload_eml.check_append_result(STATUS_NO, [None])


def test_empty_data_does_not_crash():
    with pytest.raises(upload_eml.AppendError):
        upload_eml.check_append_result(STATUS_NO, [])


def test_multipart_data_is_joined():
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload_eml.check_append_result(STATUS_NO, [b"first", b"second"])
    message = str(excinfo.value)
    assert "first" in message and "second" in message


# --- wiring: upload_eml_to_gmail against an injected fake connection ---------

GMAIL_USER = "someone@example.test"
APP_PASSWORD = "abcd efgh ijkl mnop"
SCRATCH_MAILBOX = "scratch"
SAMPLE_DATE = "Tue, 14 Nov 2023 12:00:00 +0000"
SAMPLE_EML = f"From: sender@example.test\r\nTo: {GMAIL_USER}\r\nDate: {SAMPLE_DATE}\r\nSubject: hi\r\n\r\nbody\r\n"


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: a context manager that records calls."""

    def __init__(self, status=STATUS_OK, data=None):
        self.status = status
        self.data = [b"(Success)"] if data is None else data
        self.credentials = None
        self.appends = []
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.exited = True

    def login(self, user, password):
        self.credentials = (user, password)

    def append(self, mailbox, flags, date_time, message):
        self.appends.append(
            {"mailbox": mailbox, "flags": flags, "date_time": date_time, "message": message}
        )
        return self.status, self.data


def make_factory(fake):
    """A stand-in for the IMAP4_SSL constructor, recording how it was called."""
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    factory.calls = calls
    return factory


@pytest.fixture
def eml_file(tmp_path):
    path = tmp_path / "message.eml"
    path.write_bytes(SAMPLE_EML.encode())
    return str(path)


def upload(eml_file, fake, mailbox=SCRATCH_MAILBOX):
    return upload_eml.upload_eml_to_gmail(
        eml_file, GMAIL_USER, APP_PASSWORD, mailbox, imap_factory=make_factory(fake)
    )


def test_upload_logs_in_with_the_given_credentials(eml_file):
    fake = FakeIMAP()
    upload(eml_file, fake)
    assert fake.credentials == (GMAIL_USER, APP_PASSWORD)


def test_upload_appends_the_message_bytes(eml_file):
    fake = FakeIMAP()
    upload(eml_file, fake)
    assert fake.appends[0]["message"] == SAMPLE_EML.encode()


def test_upload_appends_to_the_requested_mailbox(eml_file):
    fake = FakeIMAP()
    upload(eml_file, fake)
    assert fake.appends[0]["mailbox"] == SCRATCH_MAILBOX


def test_upload_dates_the_message_from_its_date_header(eml_file):
    """INTERNALDATE comes from Date:, not from now - computed the same way here
    so the assertion holds in any local timezone."""
    import email.utils
    import imaplib

    expected = imaplib.Time2Internaldate(
        email.utils.parsedate_to_datetime(SAMPLE_DATE).timestamp()
    )
    fake = FakeIMAP()
    upload(eml_file, fake)
    assert fake.appends[0]["date_time"] == expected


def test_upload_succeeds_when_the_server_says_ok(eml_file):
    fake = FakeIMAP(status=STATUS_OK)
    upload(eml_file, fake)


def test_upload_raises_when_the_server_says_no(eml_file):
    fake = FakeIMAP(status=STATUS_NO, data=[b"[OVERQUOTA] Not enough storage space"])
    with pytest.raises(upload_eml.AppendError):
        upload(eml_file, fake)


def test_upload_reports_the_servers_reason(eml_file):
    fake = FakeIMAP(status=STATUS_NO, data=[b"[OVERQUOTA] Not enough storage space"])
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload(eml_file, fake)
    assert "Not enough storage space" in str(excinfo.value)


def test_upload_closes_the_connection_even_when_the_append_fails(eml_file):
    """The raise must happen inside the with-block, not after it."""
    fake = FakeIMAP(status=STATUS_NO)
    with pytest.raises(upload_eml.AppendError):
        upload(eml_file, fake)
    assert fake.exited


def test_upload_connects_to_gmail_by_default(eml_file):
    fake = FakeIMAP()
    factory = make_factory(fake)
    upload_eml.upload_eml_to_gmail(
        eml_file, GMAIL_USER, APP_PASSWORD, SCRATCH_MAILBOX, imap_factory=factory
    )
    host, port = factory.calls[0]
    assert host == upload_eml.IMAP_HOST and port == upload_eml.IMAP_PORT
