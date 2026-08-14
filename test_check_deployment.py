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
