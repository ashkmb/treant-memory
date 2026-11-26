import json
import os
import shutil
import subprocess

import pytest

from treant_client import stale
from treant_client.config import Config
from treant_client.mcp_http import TreantError, TreantSession

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

F1 = "00000000-0000-0000-0000-000000000001"
F2 = "00000000-0000-0000-0000-000000000002"
F3 = "00000000-0000-0000-0000-000000000003"
F4 = "00000000-0000-0000-0000-000000000004"
F5 = "00000000-0000-0000-0000-000000000005"

FACTS = [
    {"id": F1, "type": "fact", "text": "Entry point is `old_name.py`.", "node": "demo"},
    {"id": F2, "type": "fact", "text": "Helpers are in `keep.py` and `load_config()`.", "node": "demo"},
    {"id": F3, "type": "change", "text": "Moved `old_name.py` aside.", "node": "demo"},
    {"id": F4, "type": "procedure", "text": "Templates live under `src/app/`.", "node": "demo"},
]  # fmt: skip

FLAGGED = {"flagged": True, "flag_id": 1}


class FakeSession:
    def __init__(self, results=None, raises=None, on_call=None):
        self.results = results or {}
        self.raises = raises or {}
        self.on_call = on_call
        self.calls = []
        self.opened = 0

    def factory(self, url, token, timeout):
        self.url, self.token = url, token
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.on_call:
            self.on_call(name, args)
        if name in self.raises:
            raise self.raises[name]
        return self.results[name]


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _commit(cwd, msg):
    _git(cwd, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg)


@pytest.fixture
def git_env(monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)  # no owner signing or hooks
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")


@pytest.fixture
def repo(tmp_path, git_env):
    """(path, short hash of the rename commit, full HEAD) of tmp_path/Projects/acme/demo."""
    path = tmp_path / "Projects" / "acme" / "demo"
    (path / "src" / "app").mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "old_name.py").write_text("x = 1\n")
    (path / "keep.py").write_text("y = 2\n")
    (path / "src" / "app" / "main.py").write_text("print('hi')\n")
    (path / "helper.py").write_text("def load_config(): ...\n")
    _git(path, "add", "-A")
    _commit(path, "initial")
    _git(path, "mv", "old_name.py", "new_name.py")
    _commit(path, "rename old_name to new_name")
    return path, _git(path, "rev-parse", "--short", "HEAD"), _git(path, "rev-parse", "HEAD")


@pytest.fixture
def cfg(tmp_path):
    acme = tmp_path / "Projects" / "acme"
    acme.mkdir(parents=True, exist_ok=True)
    return Config(url="https://example.invalid/mcp", folders={str(acme): "acme"})


def tok(ws):
    return "tok-test"


def _sess(facts=FACTS, flag=FLAGGED, **kw):
    return FakeSession(
        results={"repo_facts": {"facts": facts, "truncated": False}, "flag_stale": flag}, **kw
    )


def _run(path, cfg, sess, *, dry_run=False, use_memo=False, **kw):
    kw.setdefault("token_getter", tok)
    return stale.run(
        str(path), cfg, dry_run=dry_run, use_memo=use_memo, session_factory=sess.factory, **kw
    )


def _memo(tmp_path):
    p = tmp_path / "_state" / stale.MEMO_FILE
    return json.loads(p.read_text()) if p.exists() else None


def _flags(sess):
    return [args for name, args in sess.calls if name == "flag_stale"]


def test_flags_the_renamed_file_with_the_removing_commit(repo, cfg):
    path, short, head = repo
    sess = _sess()
    res = _run(path, cfg, sess)
    assert res.status == "ok" and res.flagged == 1
    assert sess.calls[0] == ("repo_facts", {"repo": "demo"})
    assert sess.calls[1] == (
        "flag_stale",
        {
            "fact_id": F1,
            "repo": "demo",
            "commit": head,
            "missing": [
                {"token": "old_name.py", "commit": short, "subject": "rename old_name to new_name"}
            ],
        },
    )
    assert len(sess.calls) == 2  # F2 present, F3 not checkable, F4 directory present
    assert res.checked == 3
    assert sess.token == "tok-test" and sess.url == cfg.url


def test_dry_run_sends_no_flags(repo, cfg):
    path, short, _ = repo
    sess = _sess()
    res = _run(path, cfg, sess, dry_run=True)
    assert res.status == "ok" and res.flagged == 0
    assert [c[0] for c in sess.calls] == ["repo_facts"]
    assert [d.fact_id for d in res.in_doubt] == [F1]
    assert res.in_doubt[0].missing == [
        {"token": "old_name.py", "commit": short, "subject": "rename old_name to new_name"}
    ]


def test_nothing_but_fact_tokens_and_commit_data_is_sent(repo, cfg):
    path, _, head = repo
    facts = [
        *FACTS,
        {"id": F5, "type": "warning", "text": "Never edit `src/gone/thing.py` or `gone_helper()`."},
    ]
    sess = _sess(facts)
    _run(path, cfg, sess)
    sent = _flags(sess)
    assert len(sent) == 2
    texts = {f["id"]: f["text"] for f in facts}
    for args in sent:
        assert set(args) == {"fact_id", "repo", "commit", "missing"}
        assert args["repo"] == "demo" and args["commit"] == head
        for m in args["missing"]:
            assert set(m) == {"token", "commit", "subject"}
            assert m["token"] in texts[args["fact_id"]]
    # strong tokens that never existed are missing without evidence
    assert sent[1]["missing"] == [
        {"token": "src/gone/thing.py", "commit": None, "subject": None},
        {"token": "gone_helper()", "commit": None, "subject": None},
    ]


def test_weak_tokens_need_a_removing_commit(repo, cfg):
    path, _, _ = repo
    facts = [
        {"id": F1, "type": "fact", "text": "Built on `TreantWidget` and `os.path` in `Brand.js`."},
        {"id": F2, "type": "fact", "text": "Docs are in `notes.md`."},
    ]
    sess = _sess(facts)
    assert _run(path, cfg, sess).flagged == 0  # never existed, no evidence: present
    (path / "notes.md").write_text("n\n")
    _git(path, "add", "notes.md")
    _commit(path, "add notes")
    _git(path, "rm", "-q", "notes.md")
    _commit(path, "drop the notes")
    short = _git(path, "rev-parse", "--short", "HEAD")
    sess = _sess(facts)
    res = _run(path, cfg, sess)
    assert res.flagged == 1
    assert _flags(sess)[0]["fact_id"] == F2
    assert _flags(sess)[0]["missing"] == [
        {"token": "notes.md", "commit": short, "subject": "drop the notes"}
    ]


def test_memo_skips_an_unchanged_head(repo, cfg, tmp_path):
    path, _, head = repo
    sess = _sess()
    assert _run(path, cfg, sess, dry_run=True, use_memo=True).status == "ok"
    assert _memo(tmp_path) is None  # dry runs never write the memo
    assert _run(path, cfg, sess, use_memo=True).status == "ok"
    assert _memo(tmp_path) == {os.path.realpath(path): head}
    assert oct((tmp_path / "_state").stat().st_mode & 0o777) == "0o700"
    sess2 = _sess()
    assert _run(path, cfg, sess2, use_memo=True).status == "skipped_unchanged"
    assert sess2.opened == 0
    (path / "more.py").write_text("z = 3\n")
    _git(path, "add", "more.py")
    _commit(path, "more")
    sess3 = _sess()
    assert _run(path, cfg, sess3, use_memo=True).status == "ok"
    assert sess3.opened == 1
    assert _memo(tmp_path) == {os.path.realpath(path): _git(path, "rev-parse", "HEAD")}
    assert _run(path, cfg, _sess(), use_memo=False).status == "ok"  # CLI form ignores it


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_budget_stops_and_does_not_remember_head(repo, cfg, tmp_path):
    path, _, _ = repo
    clock = Clock()

    def advance(name, args):
        if name == "flag_stale":
            clock.t += stale.BUDGET_S + 1

    facts = [FACTS[0], {**FACTS[0], "id": F2}]
    sess = _sess(facts, on_call=advance)
    res = _run(path, cfg, sess, use_memo=True, clock=clock)
    assert res.status == "stopped_budget"
    assert res.flagged == 1 and res.checked == 1
    assert _memo(tmp_path) is None


def test_at_most_200_facts_are_checked(repo, cfg):
    path, _, _ = repo
    facts = [
        {"id": f"00000000-0000-0000-0000-{i:012d}", "type": "fact", "text": "In `keep.py`."}
        for i in range(250)
    ]
    res = _run(path, cfg, _sess(facts))
    assert res.status == "ok" and res.checked == 200


def test_server_failure_mid_run_does_not_remember_head(repo, cfg, tmp_path):
    path, _, _ = repo
    sess = _sess(raises={"flag_stale": TreantError("unreachable")})
    res = _run(path, cfg, sess, use_memo=True)
    assert res.status == "failed_unreachable"
    assert _memo(tmp_path) is None


def test_forbidden_flag_stops_with_skipped_forbidden(repo, cfg, tmp_path):
    path, _, head = repo
    facts = [FACTS[0], {**FACTS[0], "id": F2}]
    sess = _sess(facts, raises={"flag_stale": TreantError("tool_error", detail="forbidden")})
    res = _run(path, cfg, sess, use_memo=True)
    assert res.status == "skipped_forbidden" and res.flagged == 0
    assert len(_flags(sess)) == 1  # stopped flagging
    assert _memo(tmp_path) == {os.path.realpath(path): head}


def test_per_fact_refusals_do_not_stop_the_run(repo, cfg):
    path, _, _ = repo
    facts = [FACTS[0], {**FACTS[0], "id": F2}]
    sess = _sess(facts, raises={"flag_stale": TreantError("tool_error", detail="not found")})
    res = _run(path, cfg, sess)
    assert res.status == "ok" and res.flagged == 0
    assert len(_flags(sess)) == 2


def test_already_flagged_is_not_counted(repo, cfg):
    path, _, _ = repo
    res = _run(path, cfg, _sess(flag={"flagged": False, "reason": "exists"}))
    assert res.status == "ok" and res.flagged == 0 and len(res.in_doubt) == 1


def test_empty_repo_has_no_head(tmp_path, cfg, git_env):
    path = tmp_path / "Projects" / "acme" / "empty"
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    sess = _sess()
    assert _run(path, cfg, sess).status == "no_head"
    assert sess.opened == 0


def test_not_a_repo_and_unmapped(tmp_path, cfg, git_env, monkeypatch):
    plain = tmp_path / "Projects" / "acme" / "plain"
    plain.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    sess = _sess()
    assert _run(plain, cfg, sess).status == "not_a_repo"
    assert _run(tmp_path / "nowhere", cfg, sess).status == "not_a_repo"
    other = tmp_path / "elsewhere" / "demo"
    other.mkdir(parents=True)
    _git(other, "init", "-q")
    (other / "a.py").write_text("a\n")
    _git(other, "add", "a.py")
    _commit(other, "a")
    assert _run(other, cfg, sess).status == "skipped_unmapped"
    assert sess.opened == 0


def test_no_url_and_no_token(repo, cfg):
    path, _, _ = repo
    sess = _sess()
    assert _run(path, Config(folders=cfg.folders), sess).status == "skipped_no_url"
    assert _run(path, cfg, sess, token_getter=lambda ws: None).status == "skipped_no_token"
    assert sess.opened == 0


def test_submodule_gitlink_counts_as_present(repo, cfg):
    path, _, head = repo
    _git(path, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
    _commit(path, "add vendor/lib")
    facts = [
        {"id": F1, "type": "fact", "text": "`vendor/lib/x.py` holds it"},
        {"id": F2, "type": "fact", "text": "`missing_ident_name` is set"},
    ]
    sess = _sess(facts)
    res = _run(path, cfg, sess)
    assert res.status == "ok" and res.flagged == 0
    assert _flags(sess) == []


def test_identifier_matches_a_path_stem_and_dotted_last_segment(repo, cfg):
    path, _, _ = repo
    facts = [
        {"id": F1, "type": "fact", "text": "`new_name` is the entry module"},
        {"id": F2, "type": "fact", "text": "call `config_mod.load_config()` first"},
    ]
    sess = _sess(facts)
    assert _run(path, cfg, sess).flagged == 0


def test_repo_index_has_path(repo):
    import time

    path, _, _ = repo
    idx = stale.RepoIndex.load(str(path), time.monotonic() + 10)
    for needle in ("new_name.py", "src/app/main.py", "app/main.py", "main.py", "src/app", "app"):
        assert idx.has_path(needle), needle
    for needle in ("old_name.py", "src/main.py", "rc/app"):
        assert not idx.has_path(needle), needle
    assert idx.has_ident("load_config") and not idx.has_ident("no_such_name")


def test_tool_error_carries_category():
    s = TreantSession("http://example.invalid/mcp", "t", 1)
    s.send = lambda msg: {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"structuredContent": {"error": "forbidden"}},
    }
    with pytest.raises(TreantError) as e:
        s.call_tool("flag_stale", {})
    assert e.value.category == "tool_error" and e.value.detail == "forbidden"
    assert str(e.value) == "treant tool_error"
    s2 = TreantSession("http://example.invalid/mcp", "t", 1)
    s2.send = lambda msg: {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"structuredContent": {"error": "boom"}},
    }
    with pytest.raises(TreantError) as e:
        s2.call_tool("flag_stale", {})
    assert e.value.category == "tool_error" and e.value.detail is None


def test_check_after_save_logs_and_never_raises(repo, cfg, tmp_path, monkeypatch):
    path, _, _ = repo
    sess = _sess()
    status = stale.check_after_save(str(path), cfg, token_getter=tok, session_factory=sess.factory)
    assert status == "ok"
    log = [json.loads(x) for x in (tmp_path / "_logs" / "client.log").read_text().splitlines()]
    ev = [r for r in log if r["event"] == "check_stale"]
    assert ev[-1]["workspace"] == "acme" and ev[-1]["status"] == "ok" and ev[-1]["count"] == 1
    assert "duration_ms" in ev[-1]
    assert stale.check_after_save(
        str(path), cfg, token_getter=tok, session_factory=sess.factory
    ) == ("skipped_unchanged")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(stale, "run", boom)
    assert stale.check_after_save(str(path), cfg, token_getter=tok, session_factory=boom) == (
        "failed_error"
    )


def test_removed_directory_has_its_removing_commit(repo, cfg):
    path, _, _ = repo
    _git(path, "rm", "-q", "-r", "src")
    _commit(path, "drop src")
    short = _git(path, "rev-parse", "--short", "HEAD")
    sess = _sess([FACTS[3]])
    res = _run(path, cfg, sess)
    assert res.flagged == 1
    assert _flags(sess)[0]["missing"] == [
        {"token": "src/app/", "commit": short, "subject": "drop src"}
    ]


def test_unknown_file_list_counts_everything_present(repo, monkeypatch):
    import time

    path, _, _ = repo
    real = stale._git
    monkeypatch.setattr(
        stale, "_git", lambda top, args, t: None if args[0] == "ls-tree" else real(top, args, t)
    )
    idx = stale.RepoIndex.load(str(path), time.monotonic() + 10)
    assert idx.has_path("src/gone/thing.py") and idx.has_ident("gone_helper")


def test_index_is_not_head(repo, cfg):
    """Only HEAD counts: a staged rename keeps the old path, a staged new file is absent."""
    path, _, head = repo
    _git(path, "mv", "src/app/main.py", "src/app/other.py")  # staged, not committed
    (path / "src" / "tools").mkdir(parents=True)
    (path / "src" / "tools" / "staged_only.py").write_text("s = 1\n")
    _git(path, "add", "src/tools/staged_only.py")
    facts = [
        {"id": F1, "type": "fact", "text": "Entry is `src/app/main.py`."},
        {"id": F2, "type": "fact", "text": "Run `src/tools/staged_only.py` first."},
    ]
    sess = _sess(facts)
    res = _run(path, cfg, sess)
    assert res.status == "ok" and res.flagged == 1
    [args] = _flags(sess)
    assert args["fact_id"] == F2 and args["commit"] == head
    assert args["missing"] == [
        {"token": "src/tools/staged_only.py", "commit": None, "subject": None}
    ]


def test_weak_ident_with_a_removing_commit_is_flagged(repo, cfg):
    path, _, _ = repo
    (path / "widgets.py").write_text("class OldWidget:\n    pass\n")
    _git(path, "add", "widgets.py")
    _commit(path, "add widgets")
    (path / "widgets.py").write_text("class NewWidget:\n    pass\n")
    _git(path, "add", "widgets.py")
    _commit(path, "rename the widget class")
    short = _git(path, "rev-parse", "--short", "HEAD")
    facts = [{"id": F1, "type": "fact", "text": "The base class is `OldWidget`."}]
    sess = _sess(facts)
    assert _run(path, cfg, sess).flagged == 1
    assert _flags(sess)[0]["missing"] == [
        {"token": "OldWidget", "commit": short, "subject": "rename the widget class"}
    ]


def test_glob_characters_in_a_needle_are_literal(repo):
    import time

    path, _, _ = repo
    (path / "pages").mkdir()
    (path / "pages" / "[id].tsx").write_text("x\n")
    _git(path, "add", "pages")
    _commit(path, "add page")
    _git(path, "rm", "-q", "pages/[id].tsx")
    _commit(path, "drop page")
    idx = stale.RepoIndex.load(str(path), time.monotonic() + 10)
    tok_ = stale.Token("pages/[id].tsx", "path", "pages/[id].tsx")
    assert idx.removed_in(tok_) == (_git(path, "rev-parse", "--short", "HEAD"), "drop page")
    other = stale.Token("pages/i.tsx", "path", "pages/i.tsx")  # "[id]" as a glob would match
    assert idx.removed_in(other) is None
    assert stale._glob_escape("a/[x]*?\\.py") == "a/\\[x\\]\\*\\?\\\\.py"


def test_inherited_git_dir_does_not_redirect(repo, cfg, tmp_path, monkeypatch, git_env):
    path, _, _ = repo
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    sess = _sess()
    res = _run(path, cfg, sess)
    assert res.status == "ok" and res.flagged == 1


def test_http_timeouts_are_bounded_by_the_budget(repo, cfg):
    path, _, _ = repo
    clock = Clock()
    seen = []

    class TimedSession(FakeSession):
        timeout = None

        def factory(self, url, token, timeout):
            seen.append(("open", timeout))
            return super().factory(url, token, timeout)

    def tick(name, args):
        seen.append((name, sess.timeout))
        clock.t += 25

    sess = TimedSession(
        results={"repo_facts": {"facts": FACTS, "truncated": False}, "flag_stale": FLAGGED},
        on_call=tick,
    )
    _run(path, cfg, sess, clock=clock)
    assert seen[0] == ("open", 15.0)
    assert seen[1] == ("repo_facts", 15.0)
    assert seen[2] == ("flag_stale", 5.0)  # 30 s budget, 25 s spent


def test_no_facts_skips_the_repo_index(repo, cfg, monkeypatch):
    path, _, _ = repo

    def boom(*a, **k):
        raise AssertionError("index loaded")

    monkeypatch.setattr(stale.RepoIndex, "load", boom)
    res = _run(path, cfg, _sess([]))
    assert res.status == "ok" and res.checked == 0
