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

Each deployed python file is also imported on the server, since matching digests
prove the right bytes arrived and nothing about whether they run there.

Exit codes: 1 if anything disagrees, 2 if the host could not be reached, 3 if a
deployed file is in place but does not import. They are distinct because the
responses are: go and fix something, try again later, and there is an outage.
An unreachable host is deliberately not a per-file verdict - with no answer from
the server, "missing there" is not a finding.
"""

import configparser
import hashlib
import json
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
IMPORT_FAILED = "deployed copy does not import"

REMOTE_DIGEST_COMMAND = "sha256sum"
REMOTE_PYTHON = "python3"
SSH = "ssh"
GIT = "git"
DIGEST_DISPLAY_LENGTH = 12
PYTHON_SUFFIX = ".py"

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
EXIT_CANNOT_RUN = 3

# Executed on the server to prove each deployed file still imports there.
#
# Matching digests say the right bytes arrived; they say nothing about whether
# those bytes run. This mac is on python 3.14 and the server on 3.12, which
# disagree about when annotations are evaluated (PEP 649), so a file that
# imports here can fail to import there - and for a script procmail invokes per
# delivery that is every upload dying at startup. The local suite cannot catch
# it either: pytest imports email.message itself and masks exactly that class of
# missing-submodule bug. Only a bare import does, which is what this is.
#
# `python3` is used rather than uv because uv currently resolves this project's
# `requires-python = ">=3.11"` to the system interpreter anyway (verified on the
# server: 3.12.3 both ways). Pinning a different version in a script's metadata
# block would make that untrue and this check would need to follow.
IMPORT_CHECK_BODY = """
import importlib.util, json, os, sys, traceback

results = {}
for index, path in enumerate(PATHS):
    # Siblings import each other by name, so the file's own directory has to be
    # importable exactly as it is when the script runs for real.
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    try:
        spec = importlib.util.spec_from_file_location("_deploycheck_%d" % index, path)
        if spec is None or spec.loader is None:
            results[path] = "not importable as a module"
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        results[path] = None
    except BaseException as exc:
        results[path] = traceback.format_exception_only(type(exc), exc)[-1].strip()
print(json.dumps(results))
"""


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


def import_check_program(paths: list[str]) -> bytes:
    """The program to feed the remote python, with the paths baked in.

    Baked in rather than passed as arguments so no path ever reaches a remote
    shell, where quoting would be its problem and not ours.
    """
    return (f"PATHS = {json.dumps(paths)}\n{IMPORT_CHECK_BODY}").encode()


def parse_import_output(out: str) -> dict[str, str | None] | None:
    """The per-file result the remote program printed, or None if it did not.

    Returning None matters: an empty or noisy response means the check did not
    run, which must not be read as "everything imports".
    """
    for line in reversed(out.splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def remote_import_errors(host: str, paths: list[str], run=run_command) -> dict[str, str | None]:
    """Import every remote python file in one round trip; message per failure."""
    program = import_check_program(paths)
    code, out = run([
        SSH,
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        host, REMOTE_PYTHON, "-",
    ], input=program)
    if code in (SSH_FAILURE_CODE, COMMAND_NOT_FOUND_CODE):
        raise TransportError(out.strip() or f"{SSH} exited {code}")
    parsed = parse_import_output(out)
    if parsed is None:
        return {path: f"import check produced no result: {out.strip()}" for path in paths}
    return {path: parsed.get(path, "import check did not report on this file") for path in paths}


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
    if IMPORT_FAILED in verdicts:
        return EXIT_CANNOT_RUN
    if not verdicts:
        return EXIT_DISAGREEMENT
    return EXIT_OK if all(is_clean(v) for v in verdicts) else EXIT_DISAGREEMENT


def check(host: str, files: dict[str, str], run=run_command, import_check: bool = True) -> list[dict]:
    """Compare every configured file, three ways, and prove the python imports.

    The local halves are gathered even when the server is unreachable: what is
    committed and what is in the working tree are knowable without it, and the
    answer is more useful than a row of dashes.
    """
    import_errors: dict[str, str | None] = {}
    try:
        deployed = remote_digests(host, list(files.values()), run)
        error = None
        python_files = [r for r in files.values() if r.endswith(PYTHON_SUFFIX)]
        if import_check and python_files:
            import_errors = remote_import_errors(host, python_files, run)
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
            "error": error or import_errors.get(remote),
        })
    for r in results:
        if error:
            r["verdict"] = UNREACHABLE
        elif import_errors.get(r["remote"]):
            # In sync and unrunnable is the worst case there is, so this
            # outranks whatever the digests agreed about.
            r["verdict"] = IMPORT_FAILED
        else:
            r["verdict"] = classify(r["head"], r["work"], r["deployed"])
    return results


def short(value: str | None) -> str:
    return value[:DIGEST_DISPLAY_LENGTH] if value else "-" * DIGEST_DISPLAY_LENGTH


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", "-c", default=CONFIG_PATH, help="deployment config (default: %(default)s)")
    parser.add_argument("--example-config", "-e", action="store_true", help="print a config template and exit")
    parser.add_argument("--quiet", "-q", action="store_true", help="only report files that disagree")
    parser.add_argument("--no-import-check", "-I", dest="import_check", action="store_false",
                        help="skip importing the deployed python files on the server")
    args = parser.parse_args(argv)

    if args.example_config:
        print(example_config(), end="")
        return 0

    try:
        host, files = load_deploy_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    results = check(host, files, import_check=args.import_check)
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
    elif status == EXIT_CANNOT_RUN:
        print(f"\nthe deployed copy does not import on {host} - it is in place and "
              f"cannot run:", file=sys.stderr)
        for r in results:
            if r["verdict"] == IMPORT_FAILED:
                print(f"  {r['remote']}: {r['error']}", file=sys.stderr)
    elif status:
        print(f"\n{sum(1 for r in results if not is_clean(r['verdict']))} of "
              f"{len(results)} file(s) disagree", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
