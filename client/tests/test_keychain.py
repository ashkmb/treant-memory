import subprocess

from treant_client import keychain


def _fake(rc=0, out="tok-test\n"):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    return run, calls


def test_get_token_strips_newline_and_argv():
    run, calls = _fake()
    assert keychain.get_token("alpha", run=run) == "tok-test"
    assert calls == [["security", "find-generic-password", "-s", "treant", "-a", "alpha", "-w"]]


def test_get_token_missing_is_none():
    run, _ = _fake(rc=44, out="")
    assert keychain.get_token("alpha", run=run) is None


def test_get_token_empty_is_none():
    run, _ = _fake(out="\n")
    assert keychain.get_token("alpha", run=run) is None


def test_set_token_prompts_and_never_puts_value_in_argv():
    run, calls = _fake(out="")
    assert keychain.set_token("alpha", run=run) is True
    argv = calls[0]
    assert argv == ["security", "add-generic-password", "-U", "-s", "treant", "-a", "alpha", "-w"]
    assert argv[-1] == "-w"


def test_set_token_failure():
    run, _ = _fake(rc=1, out="")
    assert keychain.set_token("alpha", run=run) is False
