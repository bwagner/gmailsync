#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Periodic housekeeping for the gmail upload path.

Run hourly from cron. Three jobs, in this order:

  1. Retry messages whose upload failed. upload_eml.py spools those; most
     failures are transient stalls, so this is where they heal themselves.
  2. Give up on anything still failing after GIVE_UP_DAYS. Such a message is
     saved into the `Stranded` mailbox, where it is visible in any IMAP client,
     and a summary is mailed to gmail so the give-up is not silent.
  3. Expire mail from INBOX older than INBOX_RETENTION_DAYS.

Step 3 is independent of 1 and 2: a failed message exists in the spool or in
`Stranded`, never only in INBOX, so expiry cannot destroy the last copy.

`--show-cron` checks whether cron and uv are usable and prints the line to
install. It names uv by absolute path: cron's PATH does not include where the
installer puts it, so the shebang alone cannot start this script from cron.

Deployed alongside upload_eml.py, which it imports - python puts the script's
own directory on sys.path, so both living in ~/bin/ is enough.
"""

import email
import email.utils
import os
import pwd
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import upload_eml

# Mailbox for messages we have stopped retrying. Visible in any IMAP client,
# which is the point - a hidden spool directory would need to be gone looking for.
STRANDED_MAILBOX = "Stranded"
INBOX_MAILBOX = "INBOX"

# How long a message keeps being retried before it is declared stranded.
GIVE_UP_DAYS = 7

# How long delivered mail stays in INBOX. Must comfortably exceed how long the
# mailbox might go unread, since gmail is the only other copy.
INBOX_RETENTION_DAYS = 30

DOVEADM = "doveadm"
CRONTAB = "crontab"
UV = "uv"
DAY_SECONDS = 86400
CRON_SCHEDULE = "0 *"  # top of every hour

# The suggested cron line runs quiet: otherwise the hourly summary is mailed by
# cron every hour, into the mailbox this job exists to keep tidy.
QUIET_FLAG = "-q"

# doveadm reads the account to act on from USER, and exits before doing anything
# if it is unset: "Fatal: USER environment is missing and -u option not used".
# cron supplies HOME, LOGNAME, PATH, LANG, SHELL and PWD - measured on the
# deployment host - and no USER, so every doveadm call needs this filled in.
USER_VAR = "USER"
LOGNAME_VAR = "LOGNAME"


@dataclass
class Outcome:
    uploaded: int = 0
    still_pending: int = 0
    stranded: list = field(default_factory=list)


def env_with_user(env) -> dict[str, str]:
    """A copy of `env` with USER filled in, for the benefit of doveadm.

    Falls back to LOGNAME, which cron does set, and then to the account owning
    this process, since nothing guarantees another scheduler sets either. If
    even that cannot be resolved the environment is returned unchanged, leaving
    doveadm to report the problem itself rather than raising here.
    """
    if env.get(USER_VAR):
        return dict(env)
    try:
        owner = env.get(LOGNAME_VAR) or pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return dict(env)
    return {**env, USER_VAR: owner}


def run_command(cmd: list[str], input: bytes | None = None) -> tuple[int, str]:
    """Run a command, returning (exit code, combined output).

    A missing binary is reported as a failed command rather than raised. This
    runs from cron, where an exception means a traceback in a mail nobody reads
    and a run abandoned before it reaches expiry; a nonzero code lets every
    caller degrade in the direction that keeps messages.
    """
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env_with_user(os.environ),
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 1, f"could not run {cmd[0]}: {exc}"
    return proc.returncode, proc.stdout.decode(errors="replace")


def pending_messages(pending_dir: Path) -> list[Path]:
    """Spooled messages awaiting retry, oldest first.

    Only completed spool files: upload_eml.py writes under a dot-prefixed
    temporary name, so a partial write is never picked up.
    """
    pending_dir = Path(pending_dir)
    if not pending_dir.is_dir():
        return []
    files = [p for p in pending_dir.iterdir() if p.suffix == upload_eml.SPOOL_SUFFIX and p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def age_days(path, now: float) -> float:
    return (now - path.stat().st_mtime) / DAY_SECONDS


def summarise(eml: bytes, age: float) -> dict:
    """Pull out just enough to identify the message in a notification."""
    msg = email.message_from_bytes(eml)
    return {
        "subject": msg.get("Subject") or "(no subject)",
        "from": msg.get("From") or "(no sender)",
        "date": msg.get("Date") or "(no date)",
        "age_days": round(age, 1),
    }


def ensure_mailbox(name: str, run=run_command) -> None:
    """Create and subscribe the mailbox if it is not already there.

    Keeps the whole thing self-contained - no manual setup step, and it
    self-heals if the folder is ever deleted from a mail client.
    """
    # `mailbox status` is the existence check. `mailbox list <name>` is NOT:
    # it echoes back any name at all with exit 0, so it reports every mailbox
    # as existing and creation would never run.
    code, _ = run([DOVEADM, "mailbox", "status", "-t", "messages", name])
    if code == 0:
        return
    run([DOVEADM, "mailbox", "create", name])
    run([DOVEADM, "mailbox", "subscribe", name])


def save_to_mailbox(eml: bytes, name: str, run=run_command) -> bool:
    """Deliver a message into a local mailbox via dovecot. True if it landed."""
    code, _ = run([DOVEADM, "save", "-m", name], input=eml)
    return code == 0


def process_pending(pending_dir: Path, upload, run=run_command, now=None,
                    give_up_days: int = GIVE_UP_DAYS) -> Outcome:
    """Retry every spooled message; strand the ones that have run out of time."""
    now = time.time() if now is None else now
    outcome = Outcome()

    for path in pending_messages(pending_dir):
        eml = path.read_bytes()
        try:
            upload(eml)
        except (upload_eml.AppendError, TimeoutError, OSError):
            age = age_days(path, now)
            if age < give_up_days:
                outcome.still_pending += 1
                continue
            # Out of time. Park it where it can be seen, and only then let go
            # of the spool copy - if the save fails this is still the only one.
            ensure_mailbox(STRANDED_MAILBOX, run)
            if save_to_mailbox(eml, STRANDED_MAILBOX, run):
                outcome.stranded.append(summarise(eml, age))
                path.unlink(missing_ok=True)
            else:
                outcome.still_pending += 1
            continue
        outcome.uploaded += 1
        path.unlink(missing_ok=True)

    return outcome


def build_notification(entries: list[dict], gmail_user: str) -> bytes:
    """A message announcing what was stranded, for uploading to gmail."""
    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = gmail_user
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Subject"] = f"mailsync: {len(entries)} message(s) could not be uploaded"
    lines = [
        (f"{len(entries)} message(s) failed to upload to gmail for more than "
         f"{GIVE_UP_DAYS} days and are no longer being retried."),
        "",
        (f"They have been placed in the '{STRANDED_MAILBOX}' mailbox on the mail "
         "server, where any IMAP client will show them."),
        "",
    ]
    for entry in entries:
        lines += [
            f"  Subject: {entry['subject']}",
            f"  From:    {entry['from']}",
            f"  Date:    {entry['date']}",
            f"  Age:     {entry['age_days']} days",
            "",
        ]
    msg.set_content("\n".join(lines))
    return msg.as_bytes()


def expire_inbox(days: int, run=run_command, dry_run: bool = False) -> tuple[int, str]:
    """Remove INBOX mail older than `days`, via dovecot so indexes stay sane."""
    criteria = ["mailbox", INBOX_MAILBOX, "savedbefore", f"{days}d"]
    verb = "search" if dry_run else "expunge"
    return run([DOVEADM, verb, *criteria])


def count_matches(code: int, out: str) -> int | None:
    """Number of messages a doveadm search matched, or None if it failed.

    Without the exit-code check a failure message counts as one match, which
    reads as "1 message would be expired" when nothing was even searched.
    """
    if code != 0:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def cron_line(script_path: str, uv_path: str) -> str:
    """The crontab line to install, naming the interpreter outright.

    Running the script by its own path relies on the `env -S uv run --script`
    shebang finding uv on PATH. Under cron it does not: cron's PATH holds no
    ~/.local/bin, where the installer puts uv, so the job dies with
    "/usr/bin/env: 'uv': No such file or directory" before python starts. An
    absolute path removes the dependency on what cron happens to provide.

    `QUIET_FLAG` goes after the script path, where it belongs to the script;
    before it, uv would take it as its own --quiet.
    """
    return f"{CRON_SCHEDULE} * * * {uv_path} run --script {script_path} {QUIET_FLAG}"


def cron_instructions(script_path: str, crontab_available: bool, uv_path: str | None) -> str:
    if not crontab_available:
        return (
            f"cron does not appear to be available to this user: '{CRONTAB}' was\n"
            "not found on PATH. Without it this script has nothing to run it\n"
            "periodically - run it by hand, or ask the administrator about cron."
        )
    if uv_path is None:
        return (
            f"cron is available, but '{UV}' was not found on PATH, so there is no\n"
            "line to suggest: the cron job would run this script through a shebang\n"
            f"that cannot resolve {UV} either. Install it first\n"
            "(https://docs.astral.sh/uv/), then run this again."
        )
    return (
        "cron is available. To run housekeeping every hour:\n"
        "\n"
        "  1. crontab -e\n"
        "  2. add this line:\n"
        "\n"
        f"     {cron_line(script_path, uv_path)}\n"
        "\n"
        "  3. save and exit; 'crontab -l' shows it, 'crontab -r' removes it all.\n"
        "\n"
        f"     {uv_path} is spelled out because cron's PATH does not include it,\n"
        "     so running the script by its own path fails in the shebang.\n"
        "\n"
        f"     {QUIET_FLAG} keeps it silent unless something is stranded or a\n"
        "     command fails; drop it to get an hourly summary by mail.\n"
    )


def crontab_available() -> bool:
    import shutil

    return shutil.which(CRONTAB) is not None


def uv_path() -> str | None:
    import shutil

    return shutil.which(UV)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--show-cron", "-c", action="store_true",
                        help="check whether cron is usable and print the line to install")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="report what would happen without uploading or deleting")
    parser.add_argument("--retention-days", "-r", type=int, default=INBOX_RETENTION_DAYS,
                        help=f"days to keep mail in {INBOX_MAILBOX} (default: %(default)s)")
    parser.add_argument("--give-up-days", "-g", type=int, default=GIVE_UP_DAYS,
                        help="days to keep retrying before stranding (default: %(default)s)")
    parser.add_argument("--quiet", "-q", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    if args.show_cron:
        print(cron_instructions(str(Path(__file__).resolve()), crontab_available(), uv_path()))
        return 0

    gmail_user, app_password = upload_eml.load_config()
    if not gmail_user or not app_password:
        print(f"No gmail credentials in {upload_eml.CONFIG_PATH}", file=sys.stderr)
        return 1

    def upload(eml: bytes, mailbox: str = INBOX_MAILBOX) -> None:
        upload_eml.upload_eml_to_gmail(eml, gmail_user, app_password, mailbox)

    if args.dry_run:
        waiting = pending_messages(upload_eml.PENDING_DIR)
        print(f"pending messages: {len(waiting)}")
        for path in waiting:
            print(f"  {path.name}  age {age_days(path, time.time()):.1f}d")
        code, out = expire_inbox(args.retention_days, dry_run=True)
        matched = count_matches(code, out)
        if matched is None:
            print(f"could not preview expiry: {out.strip()}", file=sys.stderr)
            return 1
        print(f"would expire from {INBOX_MAILBOX}: {matched} message(s) "
              f"older than {args.retention_days}d")
        return 0

    outcome = process_pending(upload_eml.PENDING_DIR, upload, give_up_days=args.give_up_days)

    if outcome.stranded:
        try:
            upload(build_notification(outcome.stranded, gmail_user))
        except (upload_eml.AppendError, TimeoutError, OSError) as exc:
            # The messages are already in Stranded; only the heads-up is lost.
            print(f"Could not send the stranded-mail notification: {exc}", file=sys.stderr)

    code, out = expire_inbox(args.retention_days)
    if code != 0:
        print(f"Expiring {INBOX_MAILBOX} failed: {out.strip()}", file=sys.stderr)

    if not args.quiet or outcome.stranded:
        print(f"retried ok: {outcome.uploaded}, still pending: {outcome.still_pending}, "
              f"stranded: {len(outcome.stranded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
