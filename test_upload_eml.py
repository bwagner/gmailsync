"""Tests for upload_eml.

Run with:   uv run --with pytest pytest -q

The repo deliberately has no pyproject.toml and no installed dependencies -
upload_eml.py is stdlib-only and deployed by copying a single file - so pytest
is supplied at run time by uv rather than declared as a project dependency.
"""

import email

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


# --- the APPENDUID gmail returns on a successful APPEND ---------------------
#
# Needed to act on the message just uploaded - labelling it with the alias it
# was addressed to - without a second round trip to find it again. Gmail returns
# it despite NOT advertising UIDPLUS, so it is observed behaviour and not a
# contract: every case where it is missing or unparseable must yield None rather
# than raise, and the caller must still treat the upload as successful.

# Verbatim from a real gmail APPEND to INBOX, observed 2026-08-13.
OBSERVED_APPEND_DATA = [b"[APPENDUID 596438452 165852] (Success)"]
OBSERVED_UIDVALIDITY = 596438452
OBSERVED_UID = 165852

# The untagged response SELECT reports the mailbox's UIDVALIDITY in.
UIDVALIDITY_CODE = "UIDVALIDITY"


def test_the_observed_gmail_response_is_parsed():
    assert upload_eml.parse_appenduid(OBSERVED_APPEND_DATA) == (596438452, 165852)


def test_the_parsed_parts_are_named_and_numeric():
    result = upload_eml.parse_appenduid(OBSERVED_APPEND_DATA)
    assert result.uidvalidity == 596438452
    assert result.uid == 165852


def test_a_response_without_appenduid_yields_none():
    """Gmail does not advertise UIDPLUS, so it owes us nothing."""
    assert upload_eml.parse_appenduid([b"(Success)"]) is None


def test_no_response_data_at_all_yields_none():
    assert upload_eml.parse_appenduid(None) is None


def test_empty_response_data_yields_none():
    assert upload_eml.parse_appenduid([]) is None


def test_a_none_inside_the_response_data_is_skipped():
    assert upload_eml.parse_appenduid([None, b"[APPENDUID 1 2]"]) == (1, 2)


def test_str_response_items_are_parsed_too():
    """imaplib hands back bytes, but nothing in its contract promises that."""
    assert upload_eml.parse_appenduid(["[APPENDUID 1 2] (Success)"]) == (1, 2)


def test_the_code_is_recognised_case_insensitively():
    assert upload_eml.parse_appenduid([b"[appenduid 1 2] (Success)"]) == (1, 2)


def test_a_uid_set_is_not_guessed_at():
    """RFC 4315 permits a uid-set here for MULTIAPPEND. This uploads one message
    at a time, so a set means the assumption broke - return nothing rather than
    pick a number and label the wrong message."""
    assert upload_eml.parse_appenduid([b"[APPENDUID 596438452 3:5] (Success)"]) is None


def test_a_malformed_code_yields_none():
    assert upload_eml.parse_appenduid([b"[APPENDUID nonsense] (Success)"]) is None


def test_undecodable_bytes_do_not_raise():
    """A garbled response must not take down a message upload."""
    assert upload_eml.parse_appenduid([b"\xff\xfe [APPENDUID 1 2]"]) == (1, 2)


# --- wiring: upload_eml_to_gmail against an injected fake connection ---------

GMAIL_USER = "someone@example.test"
APP_PASSWORD = "abcd efgh ijkl mnop"
SCRATCH_MAILBOX = "scratch"
SAMPLE_DATE = "Tue, 14 Nov 2023 12:00:00 +0000"
SAMPLE_EML = f"From: sender@example.test\r\nTo: {GMAIL_USER}\r\nDate: {SAMPLE_DATE}\r\nSubject: hi\r\n\r\nbody\r\n"


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: a context manager that records calls.

    Covers the four commands the uploader issues - login, append, select and
    uid - plus response(), which is how imaplib hands back the UIDVALIDITY that
    SELECT reported.
    """

    def __init__(
        self,
        status=STATUS_OK,
        data=None,
        select_status=STATUS_OK,
        uidvalidity=OBSERVED_UIDVALIDITY,
        store_status=STATUS_OK,
        search_status=STATUS_OK,
        search_uids=(),
    ):
        self.status = status
        self.data = [b"(Success)"] if data is None else data
        self.select_status = select_status
        self.uidvalidity = uidvalidity
        self.store_status = store_status
        self.search_status = search_status
        self.search_uids = search_uids
        self.credentials = None
        self.appends = []
        self.selects = []
        self.stores = []
        self.searches = []
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

    def select(self, mailbox):
        self.selects.append(mailbox)
        return self.select_status, [b"1"]

    def response(self, code):
        """imaplib pops the cached untagged response; [None] when there was none."""
        if code == UIDVALIDITY_CODE and self.uidvalidity is not None:
            return code, [str(self.uidvalidity).encode()]
        return code, [None]

    def uid(self, command, *args):
        if command.upper() == "STORE":
            self.stores.append(args)
            return self.store_status, [b"1 (X-GM-LABELS (...) UID 1)"]
        if command.upper() == "SEARCH":
            self.searches.append(args)
            return self.search_status, [" ".join(str(u) for u in self.search_uids).encode()]
        raise AssertionError(f"unexpected uid command {command}")


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


def test_upload_returns_the_uid_of_the_message_it_appended(eml_bytes):
    """The point of parsing it: the caller can act on the message just stored."""
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA)
    assert upload(eml_bytes, fake).appenduid == (OBSERVED_UIDVALIDITY, OBSERVED_UID)


def test_upload_succeeds_when_the_server_omits_the_uid(eml_bytes):
    """No UID is not a failure - it only means the message cannot be acted on
    without searching for it."""
    fake = FakeIMAP(data=[b"(Success)"])
    assert upload(eml_bytes, fake).appenduid is None
    assert fake.appends


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
    _, new_id = upload_eml.patch_message_id(b"Message-ID: <x@example.test>\r\n\r\nbody\r\n")
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


# --- the alias a message was addressed to, and the label for it -------------
#
# Mail reaches one mailbox through many aliases, one per correspondent, and the
# alias survives only in X-Original-To. Gmail is fed by our own APPEND rather
# than by SMTP, so it never sees that header - which is why the label has to be
# computed here. Rule and its two divergences are recorded in decisions.md,
# 2026-08-15 19:04 for the two places this deliberately diverges from it.


def message(**headers):
    """Build a message from headers alone; the body is irrelevant to the rule."""
    raw = "".join(f"{name.replace('_', '-')}: {value}\r\n" for name, value in headers.items())
    return email.message_from_string(raw + "\r\n")


# --- extracting the alias ---


def test_the_alias_comes_from_x_original_to():
    msg = message(X_Original_To="alias@example.test", To="someone@example.test")
    assert upload_eml.alias_from_headers(msg) == "alias@example.test"


def test_envelope_to_is_the_fallback():
    msg = message(Envelope_To="alias@example.test")
    assert upload_eml.alias_from_headers(msg) == "alias@example.test"


def test_x_original_to_wins_over_envelope_to():
    msg = message(X_Original_To="first@example.test", Envelope_To="second@example.test")
    assert upload_eml.alias_from_headers(msg) == "first@example.test"


def test_delivered_to_is_not_an_alias_source():
    """It holds the final hop - the real mailbox - not the alias. Using it would
    label ordinary mail with the destination it already has in common."""
    msg = message(Delivered_To="realmailbox@example.test")
    assert upload_eml.alias_from_headers(msg) is None


def test_the_to_header_is_not_an_alias_source():
    """It lies on exactly the bcc and list mail this feature exists to identify,
    and where it is truthful the alias is visible anyway."""
    msg = message(To="alias@example.test")
    assert upload_eml.alias_from_headers(msg) is None


def test_a_display_name_is_stripped_from_the_alias():
    msg = message(X_Original_To="Someone <alias@example.test>")
    assert upload_eml.alias_from_headers(msg) == "alias@example.test"


def test_the_alias_is_lowercased():
    """Addresses differing only in case must not split into two gmail labels."""
    msg = message(X_Original_To="ALIAS@Example.TEST")
    assert upload_eml.alias_from_headers(msg) == "alias@example.test"


def test_a_message_with_no_recipient_headers_has_no_alias():
    assert upload_eml.alias_from_headers(message(Subject="hi")) is None


def test_a_value_that_is_not_an_address_is_ignored():
    msg = message(X_Original_To="not-an-address")
    assert upload_eml.alias_from_headers(msg) is None


def test_a_later_header_is_used_when_the_first_is_unparseable():
    msg = message(X_Original_To="garbage", Envelope_To="alias@example.test")
    assert upload_eml.alias_from_headers(msg) == "alias@example.test"


# --- mapping an alias to a label ---


def test_the_label_puts_the_domain_before_the_localpart():
    """Domain first so gmail's sidebar groups every alias of one domain."""
    assert upload_eml.label_for_alias("alias@example.test") == "to/example.test/alias"


def test_the_label_root_is_a_single_namespace():
    assert upload_eml.label_for_alias("a@b.test").startswith(upload_eml.LABEL_ROOT + "/")


def test_a_slash_in_the_address_cannot_invent_a_nesting_level():
    """Gmail reads / as a level of nesting, so it cannot survive inside one
    component of the label."""
    assert "/" not in upload_eml.label_for_alias("a/b@c.test").removeprefix("to/c.test/")


def test_an_address_without_an_at_sign_has_no_label():
    assert upload_eml.label_for_alias("nonsense") is None


def test_no_alias_means_no_label():
    """Deliberately no to/unparsed catch-all: at upload time this should be
    unreachable, and a catch-all label would only accumulate noise."""
    assert upload_eml.label_for_alias(None) is None


def test_an_empty_localpart_has_no_label():
    assert upload_eml.label_for_alias("@example.test") is None


def test_an_empty_domain_has_no_label():
    assert upload_eml.label_for_alias("bw@") is None


# --- is the alias already visible to a human? ---


def test_an_alias_in_the_to_header_is_visible():
    msg = message(X_Original_To="alias@example.test", To="alias@example.test")
    assert upload_eml.alias_is_visible(msg, "alias@example.test")


def test_an_alias_in_the_cc_header_is_visible():
    msg = message(X_Original_To="alias@example.test", Cc="alias@example.test")
    assert upload_eml.alias_is_visible(msg, "alias@example.test")


def test_an_alias_among_several_recipients_is_still_visible():
    msg = message(X_Original_To="alias@example.test", To="a@example.test, alias@example.test, b@example.test")
    assert upload_eml.alias_is_visible(msg, "alias@example.test")


def test_visibility_ignores_case():
    msg = message(X_Original_To="alias@example.test", To="ALIAS@EXAMPLE.TEST")
    assert upload_eml.alias_is_visible(msg, "alias@example.test")


def test_an_alias_absent_from_to_and_cc_is_not_visible():
    """The bcc / mailing-list case - the whole point of the feature."""
    msg = message(X_Original_To="alias@example.test", To="list@example.test")
    assert not upload_eml.alias_is_visible(msg, "alias@example.test")


def test_a_bcc_header_does_not_count_as_visible():
    """Bcc: is not shown to the recipient, and is normally stripped entirely."""
    msg = message(X_Original_To="alias@example.test", Bcc="alias@example.test")
    assert not upload_eml.alias_is_visible(msg, "alias@example.test")


def test_nothing_is_visible_without_an_alias():
    assert not upload_eml.alias_is_visible(message(To="a@example.test"), None)


# --- the rule as a whole ---


def test_an_opaque_message_gets_the_alias_label():
    msg = message(X_Original_To="alias@example.test", To="list@example.test")
    assert upload_eml.label_for_message(msg) == "to/example.test/alias"


def test_a_message_that_already_shows_its_alias_gets_no_label():
    """A label here would duplicate the reading pane and bury the interesting
    messages. On a live sample only 4 of 26 were opaque."""
    msg = message(X_Original_To="alias@example.test", To="alias@example.test")
    assert upload_eml.label_for_message(msg) is None


def test_a_message_with_no_alias_gets_no_label():
    assert upload_eml.label_for_message(message(Subject="hi")) is None


def test_only_the_alias_label_is_produced():
    """No processed-marker label alongside it. A marker belongs to a tool with a
    finite job, and this must not start producing one nothing will own later."""
    msg = message(X_Original_To="alias@example.test", To="list@example.test")
    assert upload_eml.label_for_message(msg).startswith(upload_eml.LABEL_ROOT + "/")


def test_the_rule_works_on_a_message_parsed_from_raw_bytes():
    """The uploader has bytes, not a Message - the rule must survive the parse
    it will actually be given."""
    raw = b"X-Original-To: alias@example.test\r\nTo: list@example.test\r\nSubject: hi\r\n\r\nbody\r\n"
    assert upload_eml.label_for_message(email.message_from_bytes(raw)) == "to/example.test/alias"


# --- writing the label onto the message that was just appended --------------
#
# The label is applied over the same connection, immediately after the APPEND,
# using the UID gmail reported. Everything here is subordinate to one rule: the
# message is already delivered by the time any of this runs, so no failure in it
# may be reported as an upload failure.

EXPECTED_LABEL = "to/example.test/alias"
OPAQUE_EML = (
    "X-Original-To: alias@example.test\r\n"
    "From: sender@example.test\r\n"
    "To: list@example.test\r\n"
    f"Date: {SAMPLE_DATE}\r\n"
    "Message-ID: <abc123@example.test>\r\n"
    "Subject: hi\r\n\r\nbody\r\n"
)
OPAQUE_MESSAGE_ID = "<abc123@example.test>"
SEARCHED_UID = 4242


@pytest.fixture
def opaque_bytes():
    """A message whose alias is in no header a human reads - so it gets a label."""
    return OPAQUE_EML.encode()


# --- quoting, which is what stops a label from ending the IMAP command early ---


def test_a_value_is_wrapped_in_double_quotes():
    assert upload_eml.imap_quote("plain") == '"plain"'


def test_an_embedded_double_quote_is_escaped():
    """Unescaped, gmail reads it as the end of the string and answers BAD."""
    assert upload_eml.imap_quote('a"b') == '"a\\"b"'


def test_a_backslash_is_escaped_before_the_quotes_are():
    r"""Escaping in the other order would turn \ into \\\ and corrupt the value."""
    assert upload_eml.imap_quote("a\\b") == '"a\\\\b"'


def test_the_store_argument_is_a_parenthesised_quoted_list():
    assert upload_eml.label_store_argument(EXPECTED_LABEL) == f'("{EXPECTED_LABEL}")'


def test_a_quote_inside_a_label_cannot_close_the_list_early():
    argument = upload_eml.label_store_argument('to/example.test/a"b')
    assert argument.endswith('")') and argument.count('\\"') == 1


# --- the fallback search, for when gmail omits APPENDUID ---


def test_the_query_names_the_message_by_its_id():
    assert upload_eml.rfc822msgid_query(OPAQUE_MESSAGE_ID) == "rfc822msgid:abc123@example.test"


def test_the_angle_brackets_are_stripped():
    """Gmail's operator takes the bare id; the brackets are RFC 5322 syntax."""
    assert "<" not in upload_eml.rfc822msgid_query(OPAQUE_MESSAGE_ID)


def test_surrounding_whitespace_is_ignored():
    assert upload_eml.rfc822msgid_query("  <abc123@example.test>  ") == (
        "rfc822msgid:abc123@example.test"
    )


def test_a_missing_message_id_yields_no_query():
    assert upload_eml.rfc822msgid_query(None) is None


def test_an_empty_message_id_yields_no_query():
    assert upload_eml.rfc822msgid_query("<>") is None


def test_a_non_ascii_message_id_yields_no_query():
    """imaplib encodes the command line as ASCII, so this would raise instead of
    searching. Better no label than an exception in the delivery path."""
    assert upload_eml.rfc822msgid_query("<für@example.test>") is None


def test_search_uids_are_parsed():
    assert upload_eml.parse_search_uids([b"1 2 3"]) == [1, 2, 3]


def test_no_search_hits_parses_to_nothing():
    assert upload_eml.parse_search_uids([b""]) == []


def test_search_data_of_none_parses_to_nothing():
    assert upload_eml.parse_search_uids([None]) == []
    assert upload_eml.parse_search_uids(None) == []


def test_search_uids_given_as_str_are_parsed_too():
    assert upload_eml.parse_search_uids(["7"]) == [7]


def test_unparseable_search_tokens_are_dropped_not_raised():
    assert upload_eml.parse_search_uids([b"1 nonsense 3"]) == [1, 3]


# --- applying the label over the connection ---


# Distinguishes "the caller said nothing" from "the caller said there is no uid",
# which is the case the search fallback exists for.
UNSPECIFIED = object()


def apply(fake, label=EXPECTED_LABEL, appenduid=UNSPECIFIED, message_id=OPAQUE_MESSAGE_ID):
    if appenduid is UNSPECIFIED:
        appenduid = upload_eml.AppendUid(OBSERVED_UIDVALIDITY, OBSERVED_UID)
    return upload_eml.apply_label(fake, SCRATCH_MAILBOX, label, appenduid, message_id)


def test_the_mailbox_is_selected_before_the_store():
    """STORE is only valid in the selected state - APPEND alone does not select."""
    fake = FakeIMAP()
    apply(fake)
    assert fake.selects == [SCRATCH_MAILBOX]


def test_the_label_is_stored_on_the_appended_uid():
    fake = FakeIMAP()
    apply(fake)
    assert fake.stores == [(str(OBSERVED_UID), "+X-GM-LABELS", f'("{EXPECTED_LABEL}")')]


def test_a_stored_label_reports_success():
    assert apply(FakeIMAP()) is True


def test_a_refused_store_reports_failure_rather_than_raising():
    assert apply(FakeIMAP(store_status=STATUS_NO)) is False


def test_a_refused_select_stores_nothing():
    fake = FakeIMAP(select_status=STATUS_NO)
    assert apply(fake) is False
    assert fake.stores == []


def test_no_search_is_needed_when_gmail_reported_the_uid():
    """The whole point of APPENDUID: the happy path is one round trip, not two."""
    fake = FakeIMAP()
    apply(fake)
    assert fake.searches == []


# --- the UIDVALIDITY guard ---


def test_a_uidvalidity_mismatch_does_not_store_on_the_reported_uid():
    """A UID means nothing without its mailbox's UIDVALIDITY - and this account
    really does hand out different ones per mailbox (INBOX ...452, Trash ...453).
    Storing anyway would label whichever message now holds that number."""
    fake = FakeIMAP(uidvalidity=OBSERVED_UIDVALIDITY + 1, search_uids=(SEARCHED_UID,))
    apply(fake)
    assert fake.stores == [(str(SEARCHED_UID), "+X-GM-LABELS", f'("{EXPECTED_LABEL}")')]


def test_a_uidvalidity_mismatch_falls_back_to_the_search():
    fake = FakeIMAP(uidvalidity=OBSERVED_UIDVALIDITY + 1, search_uids=(SEARCHED_UID,))
    assert apply(fake) is True
    assert fake.searches


def test_an_unreported_uidvalidity_is_not_treated_as_a_mismatch():
    """SELECT is required to report it, so absence means an unusual server rather
    than a changed mailbox - and nothing was learned that argues against the UID."""
    fake = FakeIMAP(uidvalidity=None)
    apply(fake)
    assert fake.stores and fake.searches == []


# --- the search fallback in use ---


def test_a_missing_appenduid_searches_for_the_message():
    fake = FakeIMAP(search_uids=(SEARCHED_UID,))
    apply(fake, appenduid=None)
    assert fake.searches == [(None, "X-GM-RAW", '"rfc822msgid:abc123@example.test"')]


def test_the_searched_uid_is_the_one_labelled():
    fake = FakeIMAP(search_uids=(SEARCHED_UID,))
    apply(fake, appenduid=None)
    assert fake.stores == [(str(SEARCHED_UID), "+X-GM-LABELS", f'("{EXPECTED_LABEL}")')]


def test_a_search_finding_nothing_stores_nothing():
    fake = FakeIMAP(search_uids=())
    assert apply(fake, appenduid=None) is False
    assert fake.stores == []


def test_an_ambiguous_search_stores_nothing():
    """Message-ID should be unique and gmail dedupes on it, so several hits mean
    the assumption broke - labelling one at random would be a guess."""
    fake = FakeIMAP(search_uids=(SEARCHED_UID, SEARCHED_UID + 1))
    assert apply(fake, appenduid=None) is False
    assert fake.stores == []


def test_a_refused_search_stores_nothing():
    fake = FakeIMAP(search_status=STATUS_NO, search_uids=(SEARCHED_UID,))
    assert apply(fake, appenduid=None) is False
    assert fake.stores == []


def test_no_uid_and_no_message_id_stores_nothing():
    fake = FakeIMAP()
    assert apply(fake, appenduid=None, message_id=None) is False
    assert fake.stores == []


# --- wired into the upload ---


def test_an_opaque_message_is_labelled_by_the_upload(opaque_bytes):
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA)
    upload(opaque_bytes, fake)
    assert fake.stores == [(str(OBSERVED_UID), "+X-GM-LABELS", f'("{EXPECTED_LABEL}")')]


def test_the_result_says_what_was_applied(opaque_bytes):
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA)
    result = upload(opaque_bytes, fake)
    assert result.label == EXPECTED_LABEL
    assert result.labelled is True
    assert result.appenduid == (OBSERVED_UIDVALIDITY, OBSERVED_UID)


def test_a_message_needing_no_label_costs_no_extra_round_trip(eml_bytes):
    """The rule skips ~7 in 8 messages; those must not pay a SELECT for nothing."""
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA)
    result = upload(eml_bytes, fake)
    assert fake.selects == [] and fake.stores == []
    assert result.label is None and result.labelled is False


def test_labelling_can_be_switched_off(opaque_bytes):
    """An escape hatch that does not need a redeploy, matching --no-spool."""
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA)
    result = upload_eml.upload_eml_to_gmail(
        opaque_bytes,
        GMAIL_USER,
        APP_PASSWORD,
        SCRATCH_MAILBOX,
        imap_factory=make_factory(fake),
        apply_labels=False,
    )
    assert fake.selects == [] and result.labelled is False


def test_a_refused_label_still_reports_a_successful_upload(opaque_bytes):
    """The message is in gmail. A missing label is not worth a Program failure
    line, and spooling it would re-upload a message that already arrived."""
    fake = FakeIMAP(data=OBSERVED_APPEND_DATA, store_status=STATUS_NO)
    result = upload(opaque_bytes, fake)
    assert result.labelled is False
    assert result.appenduid == (OBSERVED_UIDVALIDITY, OBSERVED_UID)


class ExplodingLabelIMAP(FakeIMAP):
    """A connection that fails on everything the labelling step touches."""

    def select(self, mailbox):
        raise TimeoutError("timed out")


def test_an_exploding_label_step_does_not_fail_the_upload(opaque_bytes):
    """Even a timeout here: the APPEND already succeeded, so raising would turn a
    delivered message into a reported failure."""
    fake = ExplodingLabelIMAP(data=OBSERVED_APPEND_DATA)
    result = upload(opaque_bytes, fake)
    assert result.labelled is False
    assert result.appenduid == (OBSERVED_UIDVALIDITY, OBSERVED_UID)


def test_a_refused_append_is_still_an_error_and_labels_nothing(opaque_bytes):
    """Labelling must not soften the one failure that does matter."""
    fake = FakeIMAP(status=STATUS_NO, data=[b"[OVERQUOTA] Not enough storage space"])
    with pytest.raises(upload_eml.AppendError):
        upload(opaque_bytes, fake)
    assert fake.selects == [] and fake.stores == []
