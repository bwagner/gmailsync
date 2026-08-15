"""Tests for check_deployment.

Run with:   uv run --with pytest pytest -q

The remote side and git are both injected, so nothing here needs a server or a
repository.
"""

import pytest

import check_deployment as cd

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


# --- classifying one file ---------------------------------------------------
#
# Three sources, so "differs" is not one state. Each combination calls for a
# different action, and collapsing them loses exactly the information wanted.


def test_all_three_identical_is_ok():
    assert cd.classify(DIGEST_A, DIGEST_A, DIGEST_A) == cd.OK


def test_working_tree_ahead_of_head_is_uncommitted():
    assert cd.classify(DIGEST_A, DIGEST_B, DIGEST_A) == cd.UNCOMMITTED


def test_deployed_behind_head_is_undeployed():
    assert cd.classify(DIGEST_A, DIGEST_A, DIGEST_B) == cd.UNDEPLOYED


def test_deployed_matching_an_uncommitted_tree_is_called_out():
    """Shipped from the working tree without committing - the deployed version
    exists nowhere in history."""
    assert cd.classify(DIGEST_A, DIGEST_B, DIGEST_B) == cd.DEPLOYED_UNVERSIONED


def test_three_different_digests_is_drift():
    assert cd.classify(DIGEST_A, DIGEST_B, DIGEST_C) == cd.DRIFT


def test_a_missing_remote_file_is_reported_as_missing():
    assert cd.classify(DIGEST_A, DIGEST_A, None) == cd.MISSING_REMOTE


def test_a_missing_local_file_is_reported_as_missing():
    assert cd.classify(DIGEST_A, None, DIGEST_A) == cd.MISSING_LOCAL


def test_a_file_not_in_head_is_reported_as_untracked():
    assert cd.classify(None, DIGEST_A, DIGEST_A) == cd.NOT_IN_HEAD


def test_ok_is_the_only_clean_verdict():
    verdicts = {
        cd.classify(DIGEST_A, DIGEST_A, DIGEST_A),
        cd.classify(DIGEST_A, DIGEST_B, DIGEST_A),
        cd.classify(DIGEST_A, DIGEST_A, DIGEST_B),
        cd.classify(DIGEST_A, DIGEST_B, DIGEST_C),
    }
    assert len([v for v in verdicts if cd.is_clean(v)]) == 1


# --- reading the remote digests --------------------------------------------


def test_sha256sum_output_is_parsed():
    out = f"{DIGEST_A}  /home/u/bin/one.py\n{DIGEST_B}  /home/u/.procmailrc\n"
    parsed = cd.parse_digest_output(out)
    assert parsed["/home/u/bin/one.py"] == DIGEST_A
    assert parsed["/home/u/.procmailrc"] == DIGEST_B


def test_missing_remote_files_are_absent_from_the_parse():
    out = (
        f"{DIGEST_A}  /home/u/bin/one.py\n"
        "sha256sum: /home/u/bin/gone.py: No such file or directory\n"
    )
    parsed = cd.parse_digest_output(out)
    assert "/home/u/bin/one.py" in parsed
    assert len(parsed) == 1


def test_digest_output_with_leading_star_is_parsed():
    """sha256sum marks binary mode with '*' instead of a second space."""
    parsed = cd.parse_digest_output(f"{DIGEST_A} */home/u/bin/one.py\n")
    assert parsed["/home/u/bin/one.py"] == DIGEST_A


def test_remote_digests_are_fetched_in_one_call():
    """One ssh round trip for every file, not one per file."""
    calls = []

    def run(cmd, input=None):
        calls.append(cmd)
        return 0, f"{DIGEST_A}  a\n{DIGEST_B}  b\n"

    cd.remote_digests("host.test", ["a", "b"], run)
    assert len(calls) == 1


def test_remote_digests_reach_the_right_host():
    captured = {}

    def run(cmd, input=None):
        captured["cmd"] = cmd
        return 0, ""

    cd.remote_digests("host.test", ["a"], run)
    assert "host.test" in captured["cmd"]


def test_a_failed_ssh_still_yields_what_it_could_read():
    """One missing file makes sha256sum exit nonzero; the others still count."""
    def run(cmd, input=None):
        return 1, f"{DIGEST_A}  a\nsha256sum: b: No such file or directory\n"

    assert cd.remote_digests("host.test", ["a", "b"], run) == {"a": DIGEST_A}


def test_the_ssh_call_cannot_block_on_a_prompt():
    """Meant to be runnable from cron, where nothing can answer a passphrase."""
    captured = {}

    def run(cmd, input=None):
        captured["cmd"] = cmd
        return 0, ""

    cd.remote_digests("host.test", ["a"], run)
    assert "BatchMode=yes" in captured["cmd"]


def test_the_ssh_call_bounds_how_long_it_waits_to_connect():
    captured = {}

    def run(cmd, input=None):
        captured["cmd"] = cmd
        return 0, ""

    cd.remote_digests("host.test", ["a"], run)
    assert f"ConnectTimeout={cd.SSH_CONNECT_TIMEOUT_SECONDS}" in captured["cmd"]


# --- an unreachable host is not a pile of missing files ---------------------
#
# The bug this replaces: ssh failing produced no digests at all, so every file
# was classified MISSING_REMOTE - indistinguishable from someone having deleted
# the lot. ssh(1): "ssh exits with the exit status of the remote command or with
# 255 if an error occurred", so 255 is the transport failing and anything else
# came from sha256sum.


def test_a_transport_failure_is_not_reported_as_missing_files():
    def run(cmd, input=None):
        return cd.SSH_FAILURE_CODE, "ssh: connect to host host.test port 22: Operation timed out\n"

    with pytest.raises(cd.TransportError):
        cd.remote_digests("host.test", ["a", "b"], run)


def test_the_transport_failure_carries_ssh_s_own_message():
    """Timed out, refused and no-such-host are different problems."""
    def run(cmd, input=None):
        return cd.SSH_FAILURE_CODE, "ssh: Could not resolve hostname host.test\n"

    with pytest.raises(cd.TransportError) as caught:
        cd.remote_digests("host.test", ["a"], run)
    assert "Could not resolve hostname" in str(caught.value)


def test_a_missing_digest_command_is_a_transport_failure_too():
    """127 is either ssh missing here or sha256sum missing there. Either way
    nothing was learned about the remote files."""
    def run(cmd, input=None):
        return cd.COMMAND_NOT_FOUND_CODE, "bash: sha256sum: command not found\n"

    with pytest.raises(cd.TransportError):
        cd.remote_digests("host.test", ["a"], run)


def test_a_local_ssh_that_cannot_be_started_is_a_transport_failure():
    """run_command reports an unstartable command as 127, so it lands here and
    not on the files."""
    code, _ = cd.run_command(["definitely-not-a-command-xyzzy"])
    assert code == cd.COMMAND_NOT_FOUND_CODE


def test_an_unreachable_host_marks_every_file_unreachable():
    def run(cmd, input=None):
        if cmd[0] == cd.SSH:
            return cd.SSH_FAILURE_CODE, "ssh: connect to host: Operation timed out\n"
        return 1, ""

    results = cd.check("host.test", {"a.py": "bin/a.py", "b.py": "bin/b.py"}, run)
    assert [r["verdict"] for r in results] == [cd.UNREACHABLE, cd.UNREACHABLE]


def test_an_unreachable_host_reports_why():
    def run(cmd, input=None):
        if cmd[0] == cd.SSH:
            return cd.SSH_FAILURE_CODE, "ssh: Could not resolve hostname host.test\n"
        return 1, ""

    results = cd.check("host.test", {"a.py": "bin/a.py"}, run)
    assert "Could not resolve hostname" in results[0]["error"]


def test_an_unreachable_host_still_reports_the_local_side():
    """What is committed and what is in the tree are known regardless of the
    server, and are worth seeing."""
    def run(cmd, input=None):
        if cmd[0] == cd.SSH:
            return cd.SSH_FAILURE_CODE, "ssh: Operation timed out\n"
        return 1, ""

    results = cd.check("host.test", {"check_deployment.py": "bin/check_deployment.py"}, run)
    assert results[0]["work"] is not None
    assert results[0]["deployed"] is None


def test_unreachable_is_not_a_clean_verdict():
    assert not cd.is_clean(cd.UNREACHABLE)


# --- configuration ----------------------------------------------------------


CONFIG = """
[deploy]
host = host.test

[files]
upload_eml.py = bin/upload_eml.py
/elsewhere/repo/.procmailrc = .procmailrc
"""


def test_config_reads_the_host(tmp_path):
    path = tmp_path / "deploy"
    path.write_text(CONFIG)
    host, _ = cd.load_deploy_config(path)
    assert host == "host.test"


def test_config_reads_the_file_mapping(tmp_path):
    path = tmp_path / "deploy"
    path.write_text(CONFIG)
    _, files = cd.load_deploy_config(path)
    assert files["upload_eml.py"] == "bin/upload_eml.py"


def test_config_keeps_paths_from_other_repos(tmp_path):
    """.procmailrc lives in a different repository; the checker must not care."""
    path = tmp_path / "deploy"
    path.write_text(CONFIG)
    _, files = cd.load_deploy_config(path)
    assert "/elsewhere/repo/.procmailrc" in files


def test_config_preserves_case_in_paths(tmp_path):
    path = tmp_path / "deploy"
    path.write_text("[deploy]\nhost = h\n\n[files]\nCLAUDE.md = CLAUDE.md\n")
    _, files = cd.load_deploy_config(path)
    assert "CLAUDE.md" in files


def test_a_missing_config_is_an_error(tmp_path):
    with pytest.raises(cd.ConfigError):
        cd.load_deploy_config(tmp_path / "absent")


def test_a_config_without_files_is_an_error(tmp_path):
    path = tmp_path / "deploy"
    path.write_text("[deploy]\nhost = h\n")
    with pytest.raises(cd.ConfigError):
        cd.load_deploy_config(path)


def test_the_example_config_is_valid(tmp_path):
    """Whatever --example-config prints must actually parse."""
    path = tmp_path / "deploy"
    path.write_text(cd.example_config())
    host, files = cd.load_deploy_config(path)
    assert host and files


# --- exit status ------------------------------------------------------------


def test_exit_status_is_zero_when_everything_matches():
    assert cd.exit_status([cd.OK, cd.OK]) == 0


def test_exit_status_is_nonzero_on_any_drift():
    assert cd.exit_status([cd.OK, cd.UNDEPLOYED]) != 0


def test_exit_status_is_nonzero_when_nothing_was_checked():
    """An empty run must not look like a pass."""
    assert cd.exit_status([]) != 0


def test_an_unreachable_host_exits_differently_from_a_disagreement():
    """"go and fix something" and "try again later" are different answers, and
    a caller should not have to parse the output to tell them apart."""
    assert cd.exit_status([cd.UNREACHABLE]) != cd.exit_status([cd.UNDEPLOYED])


def test_an_unreachable_host_exits_with_the_cannot_check_code():
    assert cd.exit_status([cd.UNREACHABLE, cd.UNREACHABLE]) == cd.EXIT_CANNOT_CHECK


def test_a_disagreement_outranks_nothing_when_the_host_is_unreachable():
    """Every file is unreachable or none is - there is one ssh call - so a mixed
    list means the code changed underneath this assumption."""
    assert cd.exit_status([cd.OK, cd.UNREACHABLE]) == cd.EXIT_CANNOT_CHECK
