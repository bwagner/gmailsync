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

    def factory(host, port, timeout=None):
        calls.append({"host": host, "port": port, "timeout": timeout})
        return fake

    factory.calls = calls
    return factory


@pytest.fixture
def eml_bytes():
    return SAMPLE_EML.encode()


def upload(eml_bytes, fake, mailbox=SCRATCH_MAILBOX):
    return upload_eml.upload_eml_to_gmail(
        eml_bytes, GMAIL_USER, APP_PASSWORD, mailbox, imap_factory=make_factory(fake)
    )


def test_upload_logs_in_with_the_given_credentials(eml_bytes):
    fake = FakeIMAP()
    upload(eml_bytes, fake)
    assert fake.credentials == (GMAIL_USER, APP_PASSWORD)


def test_upload_appends_the_message_bytes(eml_bytes):
    fake = FakeIMAP()
    upload(eml_bytes, fake)
    assert fake.appends[0]["message"] == SAMPLE_EML.encode()


def test_upload_appends_to_the_requested_mailbox(eml_bytes):
    fake = FakeIMAP()
    upload(eml_bytes, fake)
    assert fake.appends[0]["mailbox"] == SCRATCH_MAILBOX


def test_upload_dates_the_message_from_its_date_header(eml_bytes):
    """INTERNALDATE comes from Date:, not from now - computed the same way here
    so the assertion holds in any local timezone."""
    import email.utils
    import imaplib

    expected = imaplib.Time2Internaldate(
        email.utils.parsedate_to_datetime(SAMPLE_DATE).timestamp()
    )
    fake = FakeIMAP()
    upload(eml_bytes, fake)
    assert fake.appends[0]["date_time"] == expected


def test_upload_succeeds_when_the_server_says_ok(eml_bytes):
    fake = FakeIMAP(status=STATUS_OK)
    upload(eml_bytes, fake)


def test_upload_raises_when_the_server_says_no(eml_bytes):
    fake = FakeIMAP(status=STATUS_NO, data=[b"[OVERQUOTA] Not enough storage space"])
    with pytest.raises(upload_eml.AppendError):
        upload(eml_bytes, fake)


def test_upload_reports_the_servers_reason(eml_bytes):
    fake = FakeIMAP(status=STATUS_NO, data=[b"[OVERQUOTA] Not enough storage space"])
    with pytest.raises(upload_eml.AppendError) as excinfo:
        upload(eml_bytes, fake)
    assert "Not enough storage space" in str(excinfo.value)


def test_upload_closes_the_connection_even_when_the_append_fails(eml_bytes):
    """The raise must happen inside the with-block, not after it."""
    fake = FakeIMAP(status=STATUS_NO)
    with pytest.raises(upload_eml.AppendError):
        upload(eml_bytes, fake)
    assert fake.exited


def test_upload_connects_to_gmail_by_default(eml_bytes):
    fake = FakeIMAP()
    factory = make_factory(fake)
    upload_eml.upload_eml_to_gmail(
        eml_bytes, GMAIL_USER, APP_PASSWORD, SCRATCH_MAILBOX, imap_factory=factory
    )
    call = factory.calls[0]
    assert call["host"] == upload_eml.IMAP_HOST and call["port"] == upload_eml.IMAP_PORT


# --- socket timeout ---------------------------------------------------------
#
# Without one, a hung server blocks forever. That is survivable only while the
# procmail recipe fires and forgets; once the recipe gains `w`, procmail waits
# for this process and the MTA waits with it.

# procmail's TIMEOUT default, i.e. when procmail would TERMINATE us anyway.
# The socket timeout has to be comfortably inside it to be the thing that acts.
PROCMAIL_DEFAULT_TIMEOUT_SECONDS = 960

CUSTOM_TIMEOUT = 5


def test_the_connection_is_given_a_timeout(eml_bytes):
    fake = FakeIMAP()
    factory = make_factory(fake)
    upload_eml.upload_eml_to_gmail(
        eml_bytes, GMAIL_USER, APP_PASSWORD, SCRATCH_MAILBOX, imap_factory=factory
    )
    assert factory.calls[0]["timeout"] is not None


def test_the_default_timeout_is_the_named_constant(eml_bytes):
    fake = FakeIMAP()
    factory = make_factory(fake)
    upload_eml.upload_eml_to_gmail(
        eml_bytes, GMAIL_USER, APP_PASSWORD, SCRATCH_MAILBOX, imap_factory=factory
    )
    assert factory.calls[0]["timeout"] == upload_eml.IMAP_TIMEOUT_SECONDS


def test_a_custom_timeout_is_passed_through(eml_bytes):
    fake = FakeIMAP()
    factory = make_factory(fake)
    upload_eml.upload_eml_to_gmail(
        eml_bytes,
        GMAIL_USER,
        APP_PASSWORD,
        SCRATCH_MAILBOX,
        imap_factory=factory,
        timeout=CUSTOM_TIMEOUT,
    )
    assert factory.calls[0]["timeout"] == CUSTOM_TIMEOUT


def test_the_timeout_is_positive_and_inside_procmails_ceiling():
    """Bounds rather than pins the value: it must actually bound a hang, and it
    must fire before procmail would kill us, or it buys nothing. imaplib also
    rejects a zero timeout outright."""
    assert 0 < upload_eml.IMAP_TIMEOUT_SECONDS < PROCMAIL_DEFAULT_TIMEOUT_SECONDS


class HangingIMAP(FakeIMAP):
    """Stands in for a server that accepts the connection and then stops."""

    def login(self, user, password):
        raise TimeoutError("timed out")


def test_a_timeout_propagates_out_of_the_upload(eml_bytes):
    with pytest.raises(TimeoutError):
        upload(eml_bytes, HangingIMAP())


def test_the_connection_closes_when_the_server_hangs(eml_bytes):
    fake = HangingIMAP()
    with pytest.raises(TimeoutError):
        upload(eml_bytes, fake)
    assert fake.exited


# --- reading, Message-ID patching, and the failure spool --------------------
#
# The message bytes are read by __main__ rather than inside the upload, so that
# a failed upload still has them to spool. Without that, a failure could only
# report itself; it could not preserve anything.


def test_read_eml_reads_a_file(tmp_path):
    path = tmp_path / "m.eml"
    path.write_bytes(SAMPLE_EML.encode())
    assert upload_eml.read_eml(str(path)) == SAMPLE_EML.encode()


def test_read_eml_reads_stdin_for_dash(monkeypatch):
    import io

    class FakeStdin:
        buffer = io.BytesIO(SAMPLE_EML.encode())

    monkeypatch.setattr(upload_eml.sys, "stdin", FakeStdin)
    assert upload_eml.read_eml("-") == SAMPLE_EML.encode()


def test_patch_message_id_replaces_the_header():
    original = b"From: a@example.test\r\nMessage-ID: <old@example.test>\r\n\r\nbody\r\n"
    patched, new_id = upload_eml.patch_message_id(original)
    assert b"<old@example.test>" not in patched
    assert new_id.encode() in patched


def test_patch_message_id_returns_the_id_it_used():
    patched, new_id = upload_eml.patch_message_id(b"Message-ID: <x@example.test>\r\n\r\nbody\r\n")
    assert new_id.startswith("<") and new_id.endswith(">")


def test_patch_message_id_leaves_a_message_without_one_alone():
    original = b"From: a@example.test\r\n\r\nbody\r\n"
    patched, _ = upload_eml.patch_message_id(original)
    assert patched == original


def test_spool_failed_writes_the_message(tmp_path):
    path = upload_eml.spool_failed(SAMPLE_EML.encode(), tmp_path)
    assert path.read_bytes() == SAMPLE_EML.encode()


def test_spool_failed_creates_the_directory(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    upload_eml.spool_failed(b"x", target)
    assert target.is_dir()


def test_spool_failed_leaves_no_partial_files(tmp_path):
    """Written under a temporary name and renamed, so a concurrent retry never
    picks up a half-written message."""
    upload_eml.spool_failed(SAMPLE_EML.encode(), tmp_path)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_spool_failed_does_not_collide(tmp_path):
    a = upload_eml.spool_failed(b"first", tmp_path)
    b = upload_eml.spool_failed(b"second", tmp_path)
    assert a != b
    assert {a.read_bytes(), b.read_bytes()} == {b"first", b"second"}


def test_spooled_files_are_discoverable_by_suffix(tmp_path):
    """The retry pass globs for this suffix; it has to match what is written."""
    path = upload_eml.spool_failed(b"x", tmp_path)
    assert path.suffix == upload_eml.SPOOL_SUFFIX
