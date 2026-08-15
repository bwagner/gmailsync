#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Report whether what is deployed matches what is committed.

Deployment here is a manual copy, so nothing detects drift: a file edited on the
server, or a commit never shipped, stays invisible until it matters. This
compares three versions of each file and says which pairs disagree.

    committed HEAD  vs  working tree  ->  you have uncommitted edits
    committed HEAD  vs  deployed      ->  you committed but did not ship
    working tree    vs  deployed      ->  you shipped something never committed

Collapsing those into one "differs" loses the distinction, and they call for
different actions.

Files and host come from a config file - they are specific to one machine and do
not belong in the repository. `--example-config` prints a starting point. The
mapping may name files from other repositories; each is compared against
whichever repository actually contains it.

Exits 1 if anything disagrees and 2 if the host could not be reached, so it can
be run from cron or a check. An unreachable host is deliberately not a per-file
verdict: with no answer from the server, "missing there" is not a finding.
"""

import configparser
import hashlib
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "mailsync" / "deploy"

# Verdicts.
OK = "ok"
UNCOMMITTED = "uncommitted local edits"
UNDEPLOYED = "committed but not deployed"
DEPLOYED_UNVERSIONED = "deployed from an uncommitted tree"
DRIFT = "drift: all three differ"
MISSING_REMOTE = "missing on the server"
MISSING_LOCAL = "missing locally"
NOT_IN_HEAD = "not committed"
UNREACHABLE = "host unreachable - nothing checked"

REMOTE_DIGEST_COMMAND = "sha256sum"
SSH = "ssh"
GIT = "git"
DIGEST_DISPLAY_LENGTH = 12

# ssh(1): "ssh exits with the exit status of the remote command or with 255 if
# an error occurred." So 255 is ssh itself failing - unresolvable host, refused
# connection, timeout, rejected key - and any other code came from sha256sum.
SSH_FAILURE_CODE = 255
# 127 is either ssh missing here or sha256sum missing there; both mean nothing
# was learned about the remote files.
COMMAND_NOT_FOUND_CODE = 127
# Fail in seconds rather than hanging for the system TCP timeout. The host that
# prompted this was reachable by another route, so a short wait loses nothing.
SSH_CONNECT_TIMEOUT_SECONDS = 10

EXIT_OK = 0
EXIT_DISAGREEMENT = 1
EXIT_CANNOT_CHECK = 2


class ConfigError(Exception):
    """The deployment config is missing or unusable."""


class TransportError(Exception):
    """The server could not be reached, so nothing about it is known.

    Distinct from any per-file verdict: with no answer from the host, "the file
    is missing there" is not a finding, it is an absence of findings.
    """


def run_command(cmd: list[str], input: bytes | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, input=input, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False)
    except (FileNotFoundError, PermissionError) as exc:
        return COMMAND_NOT_FOUND_CODE, f"could not run {cmd[0]}: {exc}"
    return proc.returncode, proc.stdout.decode(errors="replace")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify(head: str | None, work: str | None, deployed: str | None) -> str:
    """Name the disagreement between the three versions of one file."""
    if deployed is None:
        return MISSING_REMOTE
    if work is None:
        return MISSING_LOCAL
    if head is None:
        return NOT_IN_HEAD
    if head == work == deployed:
        return OK
    if work == head:
        return UNDEPLOYED
    if deployed == head:
        return UNCOMMITTED
    if deployed == work:
        return DEPLOYED_UNVERSIONED
    return DRIFT


def is_clean(verdict: str) -> bool:
    return verdict == OK


def parse_digest_output(out: str) -> dict[str, str]:
    """Parse `sha256sum` output, skipping its error lines for missing files."""
    digests = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        value, name = parts
        if len(value) != 64 or not all(c in "0123456789abcdef" for c in value.lower()):
            continue
        digests[name.lstrip("*").strip()] = value
    return digests


def remote_digests(host: str, paths: list[str], run=run_command) -> dict[str, str]:
    """Digest every remote file in a single ssh round trip.

    A missing file makes sha256sum exit nonzero, so most nonzero codes are
    ignored and the output parsed for whatever it did manage to read. The two
    that are not ignored mean the round trip never happened; those raise
    TransportError rather than yielding an empty result that reads as "every
    file is missing on the server".
    """
    code, out = run([
        SSH,
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        host, REMOTE_DIGEST_COMMAND, *paths,
    ])
    if code in (SSH_FAILURE_CODE, COMMAND_NOT_FOUND_CODE):
        raise TransportError(out.strip() or f"{SSH} exited {code}")
    return parse_digest_output(out)


def repo_of(path: Path, run=run_command) -> tuple[Path, str] | None:
    """The repository containing `path`, and the path relative to its root."""
    code, out = run([GIT, "-C", str(path.parent), "rev-parse", "--show-toplevel"])
    if code != 0:
        return None
    root = Path(out.strip())
    try:
        return root, str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def head_digest(path: Path, run=run_command) -> str | None:
    """Digest of the committed version, from whichever repo holds the file."""
    found = repo_of(path, run)
    if found is None:
        return None
    root, relative = found
    proc = subprocess.run([GIT, "-C", str(root), "show", f"HEAD:{relative}"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        return None
    return digest(proc.stdout)


def working_digest(path: Path) -> str | None:
    try:
        return digest(path.read_bytes())
    except OSError:
        return None


def load_deploy_config(path=CONFIG_PATH) -> tuple[str, dict[str, str]]:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no deployment config at {path} - see --example-config")
    parser = configparser.ConfigParser()
    parser.optionxform = str  # paths are case-sensitive
    parser.read(path)
    try:
        host = parser["deploy"]["host"]
        files = dict(parser["files"])
    except KeyError as exc:
        raise ConfigError(f"{path} is missing section or key: {exc}") from exc
    if not files:
        raise ConfigError(f"{path} lists no files to check")
    return host, files


def example_config() -> str:
    return (
        "# Which host to check, and which local file maps to which remote path.\n"
        "# Remote paths are relative to the remote home directory unless absolute.\n"
        "# Local paths may live in any repository - each is compared against the\n"
        "# repository that actually contains it.\n"
        "[deploy]\n"
        "host = example.test\n"
        "\n"
        "[files]\n"
        "upload_eml.py = bin/upload_eml.py\n"
        "unwrap_spam.py = bin/unwrap_spam.py\n"
        "mailsync_housekeeping.py = bin/mailsync_housekeeping.py\n"
        "# a file from another repository, given as an absolute path:\n"
        "# /path/to/mail-config/.procmailrc = .procmailrc\n"
    )


def exit_status(verdicts: list[str]) -> int:
    """Nonzero unless something was checked and all of it was clean.

    An unreachable host is its own code: "go and fix something" and "try again
    later" are different answers, and a caller should not have to read the
    output to tell them apart.
    """
    if UNREACHABLE in verdicts:
        return EXIT_CANNOT_CHECK
    if not verdicts:
        return EXIT_DISAGREEMENT
    return EXIT_OK if all(is_clean(v) for v in verdicts) else EXIT_DISAGREEMENT


def check(host: str, files: dict[str, str], run=run_command) -> list[dict]:
    """Compare every configured file, three ways.

    The local halves are gathered even when the server is unreachable: what is
    committed and what is in the working tree are knowable without it, and the
    answer is more useful than a row of dashes.
    """
    try:
        deployed = remote_digests(host, list(files.values()), run)
        error = None
    except TransportError as exc:
        deployed = {}
        error = str(exc)
    results = []
    for local, remote in files.items():
        path = Path(local).expanduser()
        results.append({
            "local": local,
            "remote": remote,
            "head": head_digest(path, run),
            "work": working_digest(path),
            "deployed": deployed.get(remote),
            "error": error,
        })
    for r in results:
        r["verdict"] = UNREACHABLE if error else classify(r["head"], r["work"], r["deployed"])
    return results


def short(value: str | None) -> str:
    return value[:DIGEST_DISPLAY_LENGTH] if value else "-" * DIGEST_DISPLAY_LENGTH


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", "-c", default=CONFIG_PATH, help="deployment config (default: %(default)s)")
    parser.add_argument("--example-config", "-e", action="store_true", help="print a config template and exit")
    parser.add_argument("--quiet", "-q", action="store_true", help="only report files that disagree")
    args = parser.parse_args(argv)

    if args.example_config:
        print(example_config(), end="")
        return 0

    try:
        host, files = load_deploy_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    results = check(host, files)
    width = max(len(r["local"]) for r in results)
    print(f"{'FILE':<{width}}  {'HEAD':<12}  {'WORKING':<12}  {'DEPLOYED':<12}  VERDICT")
    for r in results:
        if args.quiet and is_clean(r["verdict"]):
            continue
        print(f"{r['local']:<{width}}  {short(r['head'])}  {short(r['work'])}  "
              f"{short(r['deployed'])}  {r['verdict']}")

    status = exit_status([r["verdict"] for r in results])
    if status == EXIT_CANNOT_CHECK:
        print(f"\ncould not reach {host}, so nothing was compared:\n"
              f"  {results[0]['error']}", file=sys.stderr)
    elif status:
        print(f"\n{sum(1 for r in results if not is_clean(r['verdict']))} of "
              f"{len(results)} file(s) disagree", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
