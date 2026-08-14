"""Tests for mailsync_housekeeping.

Run with:   uv run --with pytest pytest -q

Every external effect is injected: `upload` stands in for the gmail APPEND and
`run` for doveadm. Nothing here touches a network, a mailbox, or a real clock.
"""

import email
import time

import pytest

import mailsync_housekeeping as hk
import upload_eml

DAY_SECONDS = 86400

GMAIL_USER = "someone@example.test"


def make_eml(subject="hi", sender="sender@example.test", date="Tue, 14 Nov 2023 12:00:00 +0000"):
    return (
        f"From: {sender}\r\nTo: {GMAIL_USER}\r\nDate: {date}\r\n"
        f"Subject: {subject}\r\n\r\nbody\r\n"
    ).encode()


class FakeRunner:
    """Records doveadm invocations and replays canned results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, cmd, input=None):
        self.calls.append({"cmd": cmd, "input": input})
        for key, value in self.results.items():
            if key in " ".join(cmd):
                return value
        return (0, "")

    def commands(self):
        return [" ".join(c["cmd"]) for c in self.calls]


class FakeUploader:
    """Stands in for the gmail APPEND. Fails for a configured number of calls."""

    def __init__(self, failures=0, exc=None):
        self.remaining_failures = failures
        self.exc = exc or upload_eml.AppendError("refused")
        self.uploaded = []

    def __call__(self, eml, mailbox="INBOX"):
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise self.exc
        self.uploaded.append(eml)


@pytest.fixture
def pending(tmp_path):
    return tmp_path / "pending"


def spool(pending_dir, eml, age_days=0.0):
    path = upload_eml.spool_failed(eml, pending_dir)
    if age_days:
        old = time.time() - age_days * DAY_SECONDS
        import os

        os.utime(path, (old, old))
    return path


# --- discovering what is pending -------------------------------------------


def test_pending_messages_finds_spooled_files(pending):
    spool(pending, make_eml())
    assert len(hk.pending_messages(pending)) == 1


def test_pending_messages_ignores_partial_writes(pending):
    spool(pending, make_eml())
    (pending / ".half.partial").write_bytes(b"incomplete")
    assert len(hk.pending_messages(pending)) == 1


def test_pending_messages_on_missing_directory_is_empty(tmp_path):
    assert hk.pending_messages(tmp_path / "nope") == []


def test_age_days_measures_file_age():
    import types

    fake = types.SimpleNamespace(stat=lambda: types.SimpleNamespace(st_mtime=0))
    assert hk.age_days(fake, now=3 * DAY_SECONDS) == pytest.approx(3)


# --- the retry pass ---------------------------------------------------------


def test_a_successful_retry_uploads_the_message(pending):
    spool(pending, make_eml(subject="recovered"))
    uploader = FakeUploader()
    hk.process_pending(pending, uploader, FakeRunner(), now=time.time())
    assert b"recovered" in uploader.uploaded[0]


def test_a_successful_retry_removes_the_spool_file(pending):
    spool(pending, make_eml())
    hk.process_pending(pending, FakeUploader(), FakeRunner(), now=time.time())
    assert hk.pending_messages(pending) == []


def test_a_failed_retry_keeps_the_message_for_next_time(pending):
    spool(pending, make_eml())
    hk.process_pending(pending, FakeUploader(failures=1), FakeRunner(), now=time.time())
    assert len(hk.pending_messages(pending)) == 1


def test_a_failed_retry_strands_nothing_while_it_is_young(pending):
    spool(pending, make_eml())
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(failures=1), runner, now=time.time())
    assert not any("save" in c for c in runner.commands())


def test_a_timeout_is_treated_as_a_retryable_failure(pending):
    spool(pending, make_eml())
    uploader = FakeUploader(failures=1, exc=TimeoutError("timed out"))
    hk.process_pending(pending, uploader, FakeRunner(), now=time.time())
    assert len(hk.pending_messages(pending)) == 1


# --- giving up --------------------------------------------------------------


def test_a_message_older_than_the_limit_is_stranded(pending):
    spool(pending, make_eml(subject="stuck"), age_days=hk.GIVE_UP_DAYS + 1)
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(failures=1), runner, now=time.time())
    assert any("save" in c for c in runner.commands())


def test_stranding_saves_to_the_stranded_mailbox(pending):
    spool(pending, make_eml(), age_days=hk.GIVE_UP_DAYS + 1)
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(failures=1), runner, now=time.time())
    save = [c for c in runner.calls if "save" in c["cmd"]][0]
    assert hk.STRANDED_MAILBOX in save["cmd"]


def test_stranding_passes_the_message_on_stdin(pending):
    eml = make_eml(subject="stuck")
    spool(pending, eml, age_days=hk.GIVE_UP_DAYS + 1)
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(failures=1), runner, now=time.time())
    save = [c for c in runner.calls if "save" in c["cmd"]][0]
    assert save["input"] == eml


def test_a_stranded_message_leaves_the_spool(pending):
    spool(pending, make_eml(), age_days=hk.GIVE_UP_DAYS + 1)
    hk.process_pending(pending, FakeUploader(failures=1), FakeRunner(), now=time.time())
    assert hk.pending_messages(pending) == []


def test_an_old_message_that_uploads_is_not_stranded(pending):
    """Age alone must not strand it - only age *and* a failed retry."""
    spool(pending, make_eml(), age_days=hk.GIVE_UP_DAYS + 1)
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(), runner, now=time.time())
    assert not any("save" in c for c in runner.commands())


def test_stranding_is_reported_with_message_details(pending):
    spool(pending, make_eml(subject="stuck", sender="who@example.test"),
          age_days=hk.GIVE_UP_DAYS + 1)
    result = hk.process_pending(pending, FakeUploader(failures=1), FakeRunner(), now=time.time())
    assert result.stranded[0]["subject"] == "stuck"
    assert "who@example.test" in result.stranded[0]["from"]


def test_a_failed_strand_keeps_the_spool_file(pending):
    """If doveadm fails we must not delete our only copy."""
    spool(pending, make_eml(), age_days=hk.GIVE_UP_DAYS + 1)
    runner = FakeRunner(results={"save": (1, "doveadm: failed")})
    hk.process_pending(pending, FakeUploader(failures=1), runner, now=time.time())
    assert len(hk.pending_messages(pending)) == 1


# --- the Stranded mailbox is created on demand ------------------------------


# `doveadm mailbox status` is the existence check, NOT `mailbox list`. Verified
# on a real server: `mailbox list DefinitelyNotAMailbox12345` echoes the name
# back with exit 0, so a list-based check reports every mailbox as existing.
# That silently skipped creation and made every strand fail.
MAILBOX_ABSENT = (68, "Mailbox doesn't exist")
MAILBOX_PRESENT = (0, "messages=1")


def test_the_mailbox_is_created_when_absent():
    runner = FakeRunner(results={"mailbox status": MAILBOX_ABSENT})
    hk.ensure_mailbox(hk.STRANDED_MAILBOX, runner)
    assert any("mailbox create" in c for c in runner.commands())


def test_the_mailbox_is_subscribed_so_clients_show_it():
    runner = FakeRunner(results={"mailbox status": MAILBOX_ABSENT})
    hk.ensure_mailbox(hk.STRANDED_MAILBOX, runner)
    assert any("mailbox subscribe" in c for c in runner.commands())


def test_an_existing_mailbox_is_not_recreated():
    runner = FakeRunner(results={"mailbox status": MAILBOX_PRESENT})
    hk.ensure_mailbox(hk.STRANDED_MAILBOX, runner)
    assert not any("mailbox create" in c for c in runner.commands())


def test_existence_is_not_decided_by_mailbox_list():
    """Regression: `mailbox list <name>` echoes any name with exit 0, so a
    list-based check always says the mailbox exists and creation never runs."""
    runner = FakeRunner(results={"mailbox status": MAILBOX_ABSENT})
    hk.ensure_mailbox(hk.STRANDED_MAILBOX, runner)
    assert not any("mailbox list" in c for c in runner.commands())


def test_the_mailbox_is_only_touched_when_something_is_stranded(pending):
    spool(pending, make_eml())
    runner = FakeRunner()
    hk.process_pending(pending, FakeUploader(), runner, now=time.time())
    assert not any("mailbox" in c for c in runner.commands())


# --- the notification -------------------------------------------------------


def test_notification_is_a_parseable_message():
    entry = {"subject": "stuck", "from": "a@example.test", "date": "then", "age_days": 8}
    msg = email.message_from_bytes(hk.build_notification([entry], GMAIL_USER))
    assert not msg.defects


def test_notification_names_every_stranded_message():
    entries = [
        {"subject": "one", "from": "a@example.test", "date": "d", "age_days": 8},
        {"subject": "two", "from": "b@example.test", "date": "d", "age_days": 9},
    ]
    body = hk.build_notification(entries, GMAIL_USER).decode()
    assert "one" in body and "two" in body


def test_notification_says_where_to_look():
    entry = {"subject": "s", "from": "f", "date": "d", "age_days": 8}
    body = hk.build_notification([entry], GMAIL_USER).decode()
    assert hk.STRANDED_MAILBOX in body


def test_notification_subject_carries_the_count():
    entries = [{"subject": "s", "from": "f", "date": "d", "age_days": 8}] * 3
    msg = email.message_from_bytes(hk.build_notification(entries, GMAIL_USER))
    assert "3" in msg.get("Subject")


# --- expiry -----------------------------------------------------------------


def test_expiry_uses_doveadm_expunge():
    runner = FakeRunner()
    hk.expire_inbox(30, runner)
    assert any("expunge" in c for c in runner.commands())


def test_expiry_targets_the_inbox_and_the_given_age():
    runner = FakeRunner()
    hk.expire_inbox(30, runner)
    cmd = runner.commands()[0]
    assert hk.INBOX_MAILBOX in cmd and "savedbefore" in cmd and "30d" in cmd


def test_expiry_can_be_previewed_without_deleting():
    runner = FakeRunner()
    hk.expire_inbox(30, runner, dry_run=True)
    assert any("search" in c for c in runner.commands())
    assert not any("expunge" in c for c in runner.commands())


# --- cron helper ------------------------------------------------------------


def test_cron_line_runs_the_script():
    line = hk.cron_line("/home/u/bin/mailsync_housekeeping.py")
    assert "/home/u/bin/mailsync_housekeeping.py" in line


def test_cron_line_is_hourly():
    assert hk.cron_line("/x").split()[0:2] == ["0", "*"]


def test_cron_line_is_quiet():
    """Without -q the job prints a summary every hour, and cron mails each one -
    24 messages a day into the very mailbox this is meant to keep tidy."""
    assert hk.cron_line("/x").endswith(hk.QUIET_FLAG)


def test_cron_instructions_mention_how_to_install():
    text = hk.cron_instructions("/x/y.py", crontab_available=True)
    assert "crontab -e" in text


def test_cron_instructions_say_so_when_cron_is_missing():
    text = hk.cron_instructions("/x/y.py", crontab_available=False)
    assert "not" in text.lower()


# --- degrading when doveadm is absent ---------------------------------------
#
# Found by running --dry-run on a machine without dovecot: subprocess raises
# FileNotFoundError, which as a cron job means a traceback instead of a message,
# and an abandoned run that never reaches expiry.

MISSING_BINARY = "doveadm-that-does-not-exist"


def test_a_missing_binary_does_not_raise():
    code, out = hk.run_command([MISSING_BINARY, "mailbox", "list"])
    assert code != 0


def test_a_missing_binary_explains_itself():
    _, out = hk.run_command([MISSING_BINARY, "mailbox", "list"])
    assert MISSING_BINARY in out


def test_stranding_keeps_the_spool_file_when_doveadm_is_missing(pending):
    """The save cannot have happened, so the spool copy is the only one left."""
    spool(pending, make_eml(), age_days=hk.GIVE_UP_DAYS + 1)

    def missing(cmd, input=None):
        return hk.run_command([MISSING_BINARY, *cmd[1:]], input=input)

    hk.process_pending(pending, FakeUploader(failures=1), missing, now=time.time())
    assert len(hk.pending_messages(pending)) == 1


def test_expiry_reports_failure_rather_than_raising():
    def missing(cmd, input=None):
        return hk.run_command([MISSING_BINARY, *cmd[1:]], input=input)

    code, _ = hk.expire_inbox(30, missing)
    assert code != 0


def test_expiry_preview_counts_matches_not_error_text():
    """A failed search must not have its error message counted as a message.
    Caught by --dry-run reporting '1 message(s)' when doveadm was absent."""
    assert hk.count_matches(1, "could not run doveadm: nope") is None
    assert hk.count_matches(0, "id1\nid2\n") == 2
    assert hk.count_matches(0, "") == 0
