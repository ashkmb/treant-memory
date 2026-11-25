import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from treant_client import kiro
from treant_client.hooks_claude import start_text
from treant_client.kiro import kiro_guard, kiro_start, kiro_sweep
from treant_client.mcp_http import TreantError

FIXTURE = Path(__file__).parent / "fixtures" / "kiro_pretooluse.json"
RISKY = "kubectl apply -f x.yaml"


class FakeSession:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result or {}, raises, []

    def factory(self, url, token, timeout):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.raises:
            raise self.raises
        return self.result


def no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, "", "")


def tok(ws):
    return "tok-test"


def _cwd(cfg):
    """A project folder inside the ``personal`` workspace."""
    root = next(f for f, w in cfg.folders.items() if w == "personal")
    p = Path(root) / "proj"
    p.mkdir(exist_ok=True)
    return str(p)


def _guard(cfg, cwd, sess, now=1000.0, command=RISKY, getter=tok, gcfg=None, **kw):
    payload = json.loads(FIXTURE.read_text())
    payload["tool_input"]["command"] = command
    kw.setdefault("run", no_git)
    return kiro_guard(
        payload,
        cwd,
        now,
        cfg,
        guard_config_loader=lambda: gcfg,
        token_getter=getter,
        session_factory=sess.factory,
        **kw,
    )


def _state():
    return json.loads((Path(os.environ["TREANT_STATE_DIR"]) / "kiro-guard.json").read_text())


def test_start_text_mapped_and_unmapped(cfg_tmp, tmp_path):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"brief": "BRIEF"})
    out = kiro_start({}, cwd, cfg_tmp, token_getter=tok, session_factory=sess.factory, run=no_git)
    assert "workspace personal" in out and out.endswith("BRIEF")
    assert out.startswith(_note(cfg_tmp) + "\n\n")
    sess2 = FakeSession({"brief": "B"})
    other = tmp_path / "elsewhere"
    other.mkdir()
    out = kiro_start(
        {}, str(other), cfg_tmp, token_getter=tok, session_factory=sess2.factory, run=no_git
    )
    assert out == "" and sess2.calls == []


def test_start_fails_open(cfg_tmp):
    sess = FakeSession(raises=TreantError("down", "network"))
    out = kiro_start(
        {}, _cwd(cfg_tmp), cfg_tmp, token_getter=tok, session_factory=sess.factory, run=no_git
    )
    assert out == _note(cfg_tmp)  # the note never depends on the server


def _note(cfg, ws="personal", suffix="Personal"):
    return f"Workspace: {ws} (folder {next(p for p in cfg.folders if p.endswith(suffix))})."


def _kstart(cfg, cwd, sess, getter=tok, **kw):
    return kiro_start(
        {}, cwd, cfg, token_getter=getter, session_factory=sess.factory, run=no_git, **kw
    )


def test_start_note_without_treant(cfg_tmp):
    sess = FakeSession({"brief": "x"})
    assert _kstart(cfg_tmp, _cwd(cfg_tmp), sess, getter=lambda ws: None) == _note(cfg_tmp)
    assert sess.calls == []


def test_start_note_with_treant_brief(cfg_tmp):
    out = _kstart(cfg_tmp, _cwd(cfg_tmp), FakeSession({"brief": "BRIEF"}))
    assert out == _note(cfg_tmp) + "\n\n" + start_text("personal", "BRIEF")


def test_start_note_server_down(cfg_tmp):
    sess = FakeSession(raises=TreantError("down", "network"))
    assert _kstart(cfg_tmp, _cwd(cfg_tmp), sess) == _note(cfg_tmp)


def test_start_note_with_about(cfg_tmp):
    from dataclasses import replace

    cfg = replace(cfg_tmp, about={"personal": "Relaxed workspace."})
    out = _kstart(cfg, _cwd(cfg), FakeSession({"brief": "B"}), getter=lambda ws: None)
    assert out == _note(cfg) + "\nRelaxed workspace."
    out = _kstart(cfg, _cwd(cfg), FakeSession({"brief": "B"}))
    assert out == _note(cfg) + "\nRelaxed workspace.\n\n" + start_text("personal", "B")


def test_start_note_unmapped_is_empty(cfg_tmp, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _kstart(cfg_tmp, str(other), FakeSession({"brief": "x"}), getter=lambda ws: None) == ""


def test_start_note_with_invalid_guard_config(tmp_path, monkeypatch):
    from treant_client import config as cc

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("not = [toml\n")
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    own = tmp_path / "c.toml"
    own.write_text('[folders]\n"~/work" = "work"\n')
    cfg = cc.load_config(own, gdir)
    out = _kstart(cfg, str(home / "work"), FakeSession(), getter=lambda w: None)
    assert out == "Workspace: work (folder ~/work)."
    empty = cc.load_config(tmp_path / "none.toml", gdir)
    assert _kstart(empty, str(home / "work"), FakeSession(), getter=lambda w: None) == ""


UNRESOLVED = "Treant guard (personal): can't tell which kube_context this uses"


def test_kiro_block_once_then_retry_allowed(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": []})
    assert _guard(cfg_tmp, cwd, sess, gcfg=guard_cfg) == (2, UNRESOLVED + kiro.RETRY_HINT)
    assert kiro.RETRY_HINT == " Re-run the same command to proceed."
    assert _guard(cfg_tmp, cwd, sess, now=1100.0, gcfg=guard_cfg) == (0, "")
    # a different command is checked afresh
    code, _ = _guard(cfg_tmp, cwd, sess, now=1100.0, command=RISKY + " --dry-run", gcfg=guard_cfg)
    assert code == 2
    key = cwd + "\0" + hashlib.sha256(RISKY.encode()).hexdigest()
    assert key in _state()
    assert RISKY not in json.dumps(_state())


def test_kiro_ttl_expiry_blocks_again(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": []})
    assert _guard(cfg_tmp, cwd, sess, now=1000.0, gcfg=guard_cfg)[0] == 2
    assert _guard(cfg_tmp, cwd, sess, now=1000.0 + 15 * 60 + 1, gcfg=guard_cfg)[0] == 2


def test_kiro_listed_and_unguarded_commands_allowed(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": []})
    assert _guard(
        cfg_tmp, cwd, sess, command="kubectl --context home get pods", gcfg=guard_cfg
    ) == (
        0,
        "",
    )
    assert _guard(cfg_tmp, cwd, sess, command="ls -la", gcfg=guard_cfg) == (0, "")
    assert [name for name, _ in sess.calls] == ["warnings"]  # ls never reached the server


def test_kiro_guard_works_without_token(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": ["never"]})
    out = _guard(cfg_tmp, cwd, sess, getter=lambda ws: None, gcfg=guard_cfg)
    assert out == (2, UNRESOLVED + kiro.RETRY_HINT)
    assert sess.calls == []


def test_kiro_warning_blocks_listed_command(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": ["careful"]})
    out = _guard(cfg_tmp, cwd, sess, command="kubectl --context home get pods", gcfg=guard_cfg)
    assert out == (2, "Treant guard (personal): warnings: careful" + kiro.RETRY_HINT)


def test_kiro_warnings_failure_keeps_local_decision(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    down = FakeSession(raises=TreantError("unreachable"))
    listed = "kubectl --context home get pods"
    assert _guard(cfg_tmp, cwd, down, command=listed, gcfg=guard_cfg) == (0, "")

    def boom(ws):
        raise RuntimeError("keychain")

    assert _guard(cfg_tmp, cwd, down, command=listed, getter=boom, gcfg=guard_cfg) == (0, "")
    assert _guard(cfg_tmp, cwd, down, gcfg=guard_cfg) == (2, UNRESOLVED + kiro.RETRY_HINT)


def test_kiro_timeout_blocks_once(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)

    def slow_run(argv, **kw):
        time.sleep(1.5)
        return subprocess.CompletedProcess(argv, 0, "home\n", "")

    sess = FakeSession({"warnings": []})
    t0 = time.monotonic()
    out = _guard(cfg_tmp, cwd, sess, gcfg=guard_cfg, run=slow_run, budget=0.3)
    assert time.monotonic() - t0 < 1.0
    assert out == (2, "Treant guard: guard timed out" + kiro.RETRY_HINT)
    assert cwd + "\0" + hashlib.sha256(RISKY.encode()).hexdigest() in _state()
    out = _guard(cfg_tmp, cwd, sess, now=1100.0, gcfg=guard_cfg, run=slow_run, budget=0.3)
    assert out == (0, "")


def test_kiro_invalid_guard_config_blocks_once(cfg_tmp, tmp_path):
    from treant_guard.config import load_config as load_guard_config

    gdir = tmp_path / "g"
    gdir.mkdir()
    (gdir / "guard.toml").write_text("not = [toml\n")
    gcfg = load_guard_config(gdir)
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": []})
    reason = f"Treant guard: guard config invalid: {gdir / 'guard.toml'}"
    assert _guard(cfg_tmp, cwd, sess, gcfg=gcfg) == (2, reason + kiro.RETRY_HINT)
    assert _guard(cfg_tmp, cwd, sess, now=1001.0, gcfg=gcfg) == (0, "")
    assert _guard(cfg_tmp, cwd, sess, command="ls", gcfg=gcfg) == (0, "")


def test_kiro_guard_error_blocks_once(cfg_tmp):
    def boom():
        raise RuntimeError("broken")

    payload = json.loads(FIXTURE.read_text())
    payload["tool_input"]["command"] = RISKY
    cwd = _cwd(cfg_tmp)
    out = kiro_guard(payload, cwd, 1000.0, cfg_tmp, guard_config_loader=boom, token_getter=tok)
    assert out == (2, "Treant guard: guard error" + kiro.RETRY_HINT)
    payload["tool_input"]["command"] = "ls -la"
    assert kiro_guard(payload, cwd, 1000.0, cfg_tmp, guard_config_loader=boom) == (0, "")


def _deny_cfg(guard_cfg):
    from dataclasses import replace

    return replace(guard_cfg, deny={"personal": {"kube_context": ["prod"]}})


DENY_CMD = "kubectl --context prod get pods"
DENIED = "Treant guard (personal): denied in personal: kube_context=prod"
DENY_SUFFIX = " Denied by the guard config; run it yourself if you mean it."


def test_kiro_deny_blocks_every_time(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    g = _deny_cfg(guard_cfg)
    sess = FakeSession({"warnings": []})
    for now in (1000.0, 1001.0):
        assert _guard(cfg_tmp, cwd, sess, now=now, command=DENY_CMD, gcfg=g) == (
            2,
            DENIED + DENY_SUFFIX,
        )
    assert sess.calls == []
    assert not (Path(os.environ["TREANT_STATE_DIR"]) / "kiro-guard.json").exists()


def test_kiro_deny_ignores_retry_state(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    g = _deny_cfg(guard_cfg)
    key = cwd + "\0" + hashlib.sha256(DENY_CMD.encode()).hexdigest()
    d = Path(os.environ["TREANT_STATE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "kiro-guard.json").write_text(json.dumps({key: 1000.0}))
    assert _guard(cfg_tmp, cwd, FakeSession(), now=1001.0, command=DENY_CMD, gcfg=g) == (
        2,
        DENIED + DENY_SUFFIX,
    )
    # an ask still passes on its identical retry
    assert _guard(cfg_tmp, cwd, FakeSession(), now=1001.0, command=RISKY, gcfg=g)[0] == 2
    assert _guard(cfg_tmp, cwd, FakeSession(), now=1002.0, command=RISKY, gcfg=g) == (0, "")


def test_kiro_corrupt_state_still_blocks(cfg_tmp, guard_cfg):
    d = Path(os.environ["TREANT_STATE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "kiro-guard.json").write_text("{not json")
    assert _guard(cfg_tmp, _cwd(cfg_tmp), FakeSession(), gcfg=guard_cfg)[0] == 2


def test_kiro_guard_logs_status(cfg_tmp, guard_cfg):
    _guard(cfg_tmp, _cwd(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg)
    log_file = Path(os.environ["TREANT_CLIENT_LOG_DIR"]) / "client.log"
    lines = [json.loads(x) for x in log_file.read_text().splitlines()]
    rec = [r for r in lines if r["event"] == "kiro_guard"]
    assert [(r["status"], r["workspace"]) for r in rec] == [("ask", "personal")]
    assert "kubectl" not in log_file.read_text()


def test_guard_non_shell_tool_allowed(cfg_tmp, guard_cfg):
    payload = json.loads(FIXTURE.read_text())
    payload["tool_name"] = "fs_write"
    sess = FakeSession({"warnings": ["x"]})
    assert kiro_guard(
        payload,
        _cwd(cfg_tmp),
        1.0,
        cfg_tmp,
        guard_config_loader=lambda: guard_cfg,
        token_getter=tok,
        session_factory=sess.factory,
    ) == (0, "")
    assert sess.calls == []


def test_guard_noop_inside_summarizer(cfg_tmp, guard_cfg, monkeypatch):
    monkeypatch.setenv("TREANT_SUMMARIZER", "1")
    sess = FakeSession({"warnings": ["w"]})
    assert _guard(cfg_tmp, _cwd(cfg_tmp), sess, gcfg=guard_cfg) == (0, "")
    assert sess.calls == []


# ---- sweep ----

NOW = 10_000_000_000
MIN = 60_000


def make_db(path, rows):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE conversations_v2 (key TEXT NOT NULL, conversation_id TEXT NOT NULL, "
        "value TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "PRIMARY KEY (key, conversation_id))"
    )
    for key, cid, value, upd in rows:
        db.execute(
            "INSERT INTO conversations_v2 VALUES (?,?,?,?,?)",
            (key, cid, json.dumps(value), upd - MIN, upd),
        )
    db.commit()
    db.close()


def _since(value=0):
    """Write the sweep watermark (epoch ms). 0 = backfill everything."""
    d = Path(os.environ["TREANT_STATE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "kiro-since").write_text(f"{value}\n")


def _log_events(tmp_path, event):
    p = tmp_path / "_logs" / "client.log"
    lines = p.read_text().splitlines() if p.exists() else []
    return [e for e in map(json.loads, lines) if e["event"] == event]


class FakeSave:
    def __init__(self, status="saved"):
        self.status, self.calls = status, []

    def __call__(self, session_id, cwd, load, trigger, cfg=None, **kw):
        self.calls.append((session_id, cwd, trigger, load()))
        return self.status if isinstance(self.status, str) else self.status(session_id)


def _rows(cfg):
    cwd = _cwd(cfg)
    v = {"transcript": ["> hello", "answer"], "history": []}
    return cwd, [
        (cwd, "idle", v, NOW - 31 * MIN),
        (cwd, "active", v, NOW - 5 * MIN),
        ("/private/tmp", "unmapped", v, NOW - 60 * MIN),
    ]


def test_sweep_saves_idle_mapped_only(cfg_tmp, tmp_path):
    _since(0)
    cwd, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    save = FakeSave()
    out = kiro_sweep(NOW, cfg_tmp, db_path=db, save=save)
    assert out == ["saved"]
    assert [(c[0], c[1], c[2]) for c in save.calls] == [("idle", cwd, "kiro_idle")]
    assert save.calls[0][3] == ("> hello\nanswer", 0)


def test_sweep_does_not_resave_unchanged(cfg_tmp, tmp_path):
    _since(0)
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    save = FakeSave()
    kiro_sweep(NOW, cfg_tmp, db_path=db, save=save)
    assert kiro_sweep(NOW + MIN, cfg_tmp, db_path=db, save=save) == []
    assert len(save.calls) == 1
    # the active one becomes idle later and is saved then; the idle one stays saved
    out = kiro_sweep(NOW + 40 * MIN, cfg_tmp, db_path=db, save=save)
    assert out == ["saved"] and save.calls[-1][0] == "active"
    # new activity on the saved one re-saves it
    d = sqlite3.connect(db)
    d.execute(
        "UPDATE conversations_v2 SET updated_at=? WHERE conversation_id='idle'", (NOW + 50 * MIN,)
    )
    d.commit()
    d.close()
    assert kiro_sweep(NOW + 90 * MIN, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert save.calls[-1][0] == "idle"


def test_sweep_retries_failures(cfg_tmp, tmp_path):
    _since(0)
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    save = FakeSave("failed_network")
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["failed_network"]
    save.status = "saved"
    assert kiro_sweep(NOW + MIN, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert len(save.calls) == 2


def test_sweep_skipped_status_is_recorded(cfg_tmp, tmp_path):
    _since(0)
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    save = FakeSave("skipped_too_short")
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["skipped_too_short"]
    assert kiro_sweep(NOW + MIN, cfg_tmp, db_path=db, save=save) == []


def test_sweep_missing_db_or_bad_value(cfg_tmp, tmp_path):
    assert kiro_sweep(NOW, cfg_tmp, db_path=tmp_path / "nope.sqlite3", save=FakeSave()) == []
    _since(0)
    cwd = _cwd(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, [(cwd, "c1", {}, NOW - 40 * MIN)])
    d = sqlite3.connect(db)
    d.execute("UPDATE conversations_v2 SET value='{broken'")
    d.commit()
    d.close()
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == []  # unparseable: skipped, no crash


def test_sweep_noop_inside_summarizer(cfg_tmp, tmp_path, monkeypatch):
    _since(0)
    monkeypatch.setenv("TREANT_SUMMARIZER", "1")
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=FakeSave()) == []


def test_db_opened_read_only(tmp_path):
    db = tmp_path / "k.sqlite3"
    make_db(db, [])
    conn = kiro._open_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM conversations_v2")
    conn.close()


def test_sweep_summarizer_failure_not_recorded(cfg_tmp, tmp_path, monkeypatch):
    _since(0)
    from test_summarize import LAUNCHD_PATH, launchd_run

    from treant_client.summarize import summarize

    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    cwd = _cwd(cfg_tmp)
    long = {"transcript": ["> question " + "x" * 2000, "answer"], "history": [1, 2, 3]}
    db = tmp_path / "k.sqlite3"
    make_db(db, [(cwd, "c1", long, NOW - 40 * MIN)])
    out = kiro_sweep(
        NOW,
        cfg_tmp,
        db_path=db,
        token_getter=tok,
        summarizer=lambda t: summarize(t, run=launchd_run),
    )
    assert out == ["failed_summarize"]
    saved = Path(os.environ["TREANT_STATE_DIR"]) / "kiro-saved.json"
    assert not saved.exists() or "c1" not in json.loads(saved.read_text())


# ---- no backfill: the kiro-since watermark ----


def test_sweep_skips_rows_older_than_watermark(cfg_tmp, tmp_path):
    cwd = _cwd(cfg_tmp)
    v = {"transcript": ["> hi"], "history": []}
    db = tmp_path / "k.sqlite3"
    make_db(db, [(cwd, "old", v, NOW - 50 * MIN), (cwd, "new", v, NOW - 31 * MIN)])
    _since(NOW - 40 * MIN)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert [c[0] for c in save.calls] == ["new"]
    saved = json.loads((Path(os.environ["TREANT_STATE_DIR"]) / "kiro-saved.json").read_text())
    assert "old" not in saved
    assert kiro_sweep(NOW + 60 * MIN, cfg_tmp, db_path=db, save=save) == []
    assert [c[0] for c in save.calls] == ["new"]


def test_sweep_missing_watermark_writes_now_and_saves_nothing(cfg_tmp, tmp_path):
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == []
    assert save.calls == []
    since = Path(os.environ["TREANT_STATE_DIR"]) / "kiro-since"
    assert int(since.read_text()) == NOW
    # activity after the watermark is saved once idle; history before it never is
    d = sqlite3.connect(db)
    d.execute(
        "UPDATE conversations_v2 SET updated_at=? WHERE conversation_id='active'", (NOW + MIN,)
    )
    d.commit()
    d.close()
    assert kiro_sweep(NOW + 40 * MIN, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert [c[0] for c in save.calls] == ["active"]
    assert int(since.read_text()) == NOW


def test_sweep_corrupt_watermark_counts_as_missing(cfg_tmp, tmp_path):
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    _since("garbage")
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == []
    assert int((Path(os.environ["TREANT_STATE_DIR"]) / "kiro-since").read_text()) == NOW


def test_sweep_reads_value_only_for_rows_it_saves(cfg_tmp, tmp_path, monkeypatch):
    cwd = _cwd(cfg_tmp)
    v = {"transcript": ["> hi"], "history": []}
    db = tmp_path / "k.sqlite3"
    make_db(
        db,
        [
            (cwd, "old", v, NOW - 50 * MIN),
            (cwd, "new", v, NOW - 31 * MIN),
            ("/private/tmp", "unmapped", v, NOW - 31 * MIN),
        ],
    )
    d = sqlite3.connect(db)
    d.execute("UPDATE conversations_v2 SET value='{broken' WHERE conversation_id='old'")
    d.commit()
    d.close()
    _since(NOW - 40 * MIN)
    statements = []
    real_open = kiro._open_ro

    def traced(path):
        conn = real_open(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(kiro, "_open_ro", traced)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["saved"]
    listing = [x for x in statements if "FROM conversations_v2" in x and "value" not in x]
    reads = [x for x in statements if "SELECT value" in x]
    assert len(listing) == 1
    assert len(reads) == 1 and "'new'" in reads[0]
    assert _log_events(tmp_path, "kiro_sweep") == []


# ---- one bad row never aborts the sweep ----

DEEP = "[" * 100_000 + "]" * 100_000


def make_loose_db(path, rows):
    """A conversations_v2 without type or NOT NULL constraints, to hold odd rows."""
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE conversations_v2 (key, conversation_id, value, created_at, updated_at, "
        "PRIMARY KEY (key, conversation_id))"
    )
    db.executemany("INSERT INTO conversations_v2 VALUES (?,?,?,0,?)", rows)
    db.commit()
    db.close()


def test_sweep_survives_bad_rows(cfg_tmp, tmp_path):
    cwd = _cwd(cfg_tmp)
    good = json.dumps({"transcript": ["> hi"], "history": []})
    t = NOW - 40 * MIN
    db = tmp_path / "k.sqlite3"
    make_loose_db(
        db,
        [
            (cwd, "none_updated", good, None),
            (cwd, "text_updated", good, "yesterday"),
            (cwd, "float_updated", good, t + 0.5),
            (cwd + "\0evil", "nul_key", good, t),
            (cwd, 7, good, t),
            (cwd, "deep", DEEP, t),
            (cwd, "null_value", None, t),
            (cwd, "good", good, t),
        ],
    )
    _since(0)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert [c[0] for c in save.calls] == ["good"]
    saved = json.loads((Path(os.environ["TREANT_STATE_DIR"]) / "kiro-saved.json").read_text())
    assert set(saved) == {"good"}
    events = _log_events(tmp_path, "kiro_sweep")
    assert {e["category"] for e in events} == {"TypeError", "ValueError", "RecursionError"}
    assert all(set(e) == {"ts", "event", "status", "category"} for e in events)
    assert all(e["status"] == "error" for e in events)
    # failed rows were not recorded: the next sweep tries them again, still without aborting
    assert kiro_sweep(NOW + MIN, cfg_tmp, db_path=db, save=save) == []
    assert len(_log_events(tmp_path, "kiro_sweep")) == 2 * len(events)


def test_sweep_survives_a_raising_save(cfg_tmp, tmp_path):
    cwd = _cwd(cfg_tmp)
    v = {"transcript": ["> hi"], "history": []}
    db = tmp_path / "k.sqlite3"
    make_db(db, [(cwd, "a", v, NOW - 40 * MIN), (cwd, "b", v, NOW - 35 * MIN)])
    _since(0)

    def save(session_id, **kw):
        if session_id == "a":
            raise RuntimeError("boom")
        return "saved"

    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=save) == ["saved"]
    assert [e["category"] for e in _log_events(tmp_path, "kiro_sweep")] == ["RuntimeError"]


def test_start_note_server_slow(cfg_tmp):
    class Slow(FakeSession):
        def call_tool(self, name, args):
            time.sleep(2)
            return self.result

    out = _kstart(cfg_tmp, _cwd(cfg_tmp), Slow({"brief": "x"}), budget=0.3)
    assert out == _note(cfg_tmp)


def test_start_slow_config_step_stays_within_budget(cfg_tmp, monkeypatch):
    def slow_cfg(cfg):
        time.sleep(2)
        return cfg

    monkeypatch.setattr("treant_client.hooks_claude._cfg", slow_cfg)  # the shared start flow
    t0 = time.monotonic()
    assert _kstart(cfg_tmp, _cwd(cfg_tmp), FakeSession({"brief": "x"}), budget=0.3) == ""
    assert time.monotonic() - t0 < 1.0


def test_kiro_deny_without_a_reason_still_blocks(cfg_tmp, guard_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr("treant_client.kiro.guard_command", lambda *a, **k: ("deny", None))
    state = tmp_path / "state"
    code, msg = _guard(cfg_tmp, str(tmp_path), FakeSession(), gcfg=guard_cfg, state_dir=state)
    assert code == 2
    assert msg == "Treant guard: denied" + kiro.DENY_SUFFIX
    assert not (state / "kiro-guard.json").exists()


# --- Kiro v3 sessions (~/.kiro/sessions/<hash>/<id>/session.json + messages.jsonl) -----------


@pytest.fixture(autouse=True)
def _no_real_kiro_sessions(tmp_path, monkeypatch):
    """Never let a sweep in these tests read the owner's real ~/.kiro/sessions."""
    monkeypatch.setattr(kiro, "KIRO_SESSIONS", tmp_path / "_no_kiro_sessions")


def _iso(ms):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def _msg(type, content=None, **kw):
    return json.dumps(
        {"id": "m", "timestamp": "t", "payload": {"type": type, "content": content, **kw}}
    )


V3_MESSAGES = [
    _msg("user", "hello there", source="chat"),
    _msg("user", "steering doc text", source="steer"),
    _msg("assistant", "thinking hard", operationType="Reasoning"),
    _msg("assistant", "hi, on it", operationType="Say"),
    _msg("tool_call", toolName="shell", args={"command": "ls"}),
    _msg("tool_result", "file-a"),
    "not json at all",
    _msg("user", "thanks"),
    _msg("turn_end"),
]


def make_v3(root, sid, cwd, updated_ms, messages=V3_MESSAGES, hash="abc123"):
    d = root / hash / sid
    d.mkdir(parents=True)
    (d / "session.json").write_text(
        json.dumps({"id": sid, "workspacePaths": [cwd], "lastModifiedAt": _iso(updated_ms)})
    )
    (d / "messages.jsonl").write_text("\n".join(messages) + "\n")
    return d


def test_v3_text_keeps_chat_and_tool_names_only(tmp_path):
    from treant_client.transcript import kiro_v3_text

    d = make_v3(tmp_path, "sess_1", "/x", NOW)
    text, turns = kiro_v3_text(d / "messages.jsonl")
    assert text == "USER: hello there\nASSISTANT: hi, on it\n[tool shell]\nUSER: thanks"
    assert turns == 2


def test_sweep_saves_idle_v3_sessions(cfg_tmp, tmp_path):
    _since(NOW - 120 * MIN)
    root = tmp_path / "sessions"
    cwd = _cwd(cfg_tmp)
    make_v3(root, "sess_idle", cwd, NOW - 31 * MIN)
    make_v3(root, "sess_active", cwd, NOW - 5 * MIN)
    make_v3(root, "sess_unmapped", "/private/tmp", NOW - 60 * MIN)
    make_v3(root, "sess_old", cwd, NOW - 200 * MIN, hash="def456")
    (root / "cli").mkdir()  # Kiro's prompt-history folder: no session.json, ignored
    (root / "cli" / "x.history").write_text("ls\n")
    save = FakeSave()
    out = kiro_sweep(
        NOW, cfg_tmp, db_path=tmp_path / "missing.sqlite3", sessions_dir=root, save=save
    )
    assert out == ["saved"]
    sid, got_cwd, trigger, (text, turns) = save.calls[0]
    assert (sid, got_cwd, trigger, turns) == ("sess_idle", cwd, "kiro_idle", 2)
    assert text.startswith("USER: hello there")
    assert (
        kiro_sweep(NOW + MIN, cfg_tmp, db_path=tmp_path / "m", sessions_dir=root, save=save) == []
    )
    # the active one goes idle; then new activity on the saved one saves it again
    later = kiro_sweep(
        NOW + 40 * MIN, cfg_tmp, db_path=tmp_path / "m", sessions_dir=root, save=save
    )
    assert later == ["saved"] and save.calls[-1][0] == "sess_active"
    make_v3(root, "sess_idle", cwd, NOW + 50 * MIN, hash="moved")  # same id, newer
    again = kiro_sweep(
        NOW + 90 * MIN, cfg_tmp, db_path=tmp_path / "m", sessions_dir=root, save=save
    )
    assert again == ["saved"] and save.calls[-1][0] == "sess_idle"


def test_sweep_v3_odd_sessions_are_skipped(cfg_tmp, tmp_path):
    _since(0)
    root = tmp_path / "sessions"
    cwd = _cwd(cfg_tmp)
    bad = root / "h" / "sess_bad"
    bad.mkdir(parents=True)
    (bad / "session.json").write_text("{not json")
    nopath = make_v3(root, "sess_nopath", cwd, NOW - 40 * MIN)
    (nopath / "session.json").write_text(
        json.dumps({"id": "sess_nopath", "lastModifiedAt": _iso(NOW - 40 * MIN)})
    )
    make_v3(root, "sess_ok", cwd, NOW - 40 * MIN)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=tmp_path / "m", sessions_dir=root, save=save) == [
        "saved"
    ]
    assert [c[0] for c in save.calls] == ["sess_ok"]


def test_sweep_reads_both_stores(cfg_tmp, tmp_path):
    _since(0)
    cwd, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    root = tmp_path / "sessions"
    make_v3(root, "sess_v3", cwd, NOW - 40 * MIN)
    save = FakeSave()
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, sessions_dir=root, save=save) == ["saved", "saved"]
    assert sorted(c[0] for c in save.calls) == ["idle", "sess_v3"]


def test_v3_session_without_messages_is_empty(tmp_path):
    from treant_client.transcript import kiro_v3_text

    d = make_v3(tmp_path, "sess_empty", "/x", NOW)
    (d / "messages.jsonl").unlink()  # Kiro opened and closed without a message
    assert kiro_v3_text(d / "messages.jsonl") == ("", 0)


def test_v3_text_drops_the_resume_briefing(tmp_path):
    from treant_client.transcript import RESUME_MARKER, kiro_v3_text

    msgs = [_msg("user", f"{RESUME_MARKER} context only\n\n## Warnings\n- x"), *V3_MESSAGES]
    d = make_v3(tmp_path, "sess_1", "/x", NOW, messages=msgs)
    text, turns = kiro_v3_text(d / "messages.jsonl")
    assert RESUME_MARKER not in text and "Warnings" not in text
    assert turns == 2


def test_resume_brief_marks_the_start_text(monkeypatch):
    from treant_client.transcript import RESUME_MARKER

    monkeypatch.setattr(kiro, "kiro_start", lambda payload, cwd, cfg=None: f"note for {cwd}")
    out = kiro.resume_brief("/w")
    assert out.startswith(RESUME_MARKER)
    assert out.endswith("\n\nnote for /w")


def test_resume_brief_is_empty_when_start_is_empty(monkeypatch):
    monkeypatch.setattr(kiro, "kiro_start", lambda payload, cwd, cfg=None: "")
    assert kiro.resume_brief("/unmapped") == ""


def test_sweep_survives_a_deeply_nested_saved_state(cfg_tmp, tmp_path):
    # kiro-saved.json nested past the recursion limit reads as empty instead of stopping
    # every sweep with a RecursionError
    _since(0)
    _, rows = _rows(cfg_tmp)
    db = tmp_path / "k.sqlite3"
    make_db(db, rows)
    (Path(os.environ["TREANT_STATE_DIR"]) / "kiro-saved.json").write_text("[" * 100_000)
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=FakeSave()) == ["saved"]


def test_db_path_with_uri_characters(cfg_tmp, tmp_path):
    # ?, # and % in the path must not be read as URI syntax
    _since(0)
    _, rows = _rows(cfg_tmp)
    folder = tmp_path / "odd ?#% name"
    folder.mkdir()
    db = folder / "k.sqlite3"
    make_db(db, rows)
    assert kiro_sweep(NOW, cfg_tmp, db_path=db, save=FakeSave()) == ["saved"]


def test_kiro_identical_retry_skips_the_warnings_lookup(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": []})
    assert _guard(cfg_tmp, cwd, sess, gcfg=guard_cfg)[0] == 2
    calls = len(sess.calls)
    assert calls == 1
    assert _guard(cfg_tmp, cwd, sess, now=1100.0, gcfg=guard_cfg) == (0, "")
    assert len(sess.calls) == calls  # the retry passes either way: no HTTP call for it


def test_kiro_retry_of_a_warnings_ask_still_passes(cfg_tmp, guard_cfg):
    cwd = _cwd(cfg_tmp)
    sess = FakeSession({"warnings": ["careful"]})
    listed = "kubectl --context home get pods"
    assert _guard(cfg_tmp, cwd, sess, command=listed, gcfg=guard_cfg)[0] == 2
    assert _guard(cfg_tmp, cwd, sess, now=1001.0, command=listed, gcfg=guard_cfg) == (0, "")


def test_kiro_state_write_failure_blocks_and_logs_once(cfg_tmp, guard_cfg, tmp_path):
    state = tmp_path / "ro"
    state.mkdir()
    state.chmod(0o500)  # the state file cannot be written
    try:
        out = _guard(
            cfg_tmp, _cwd(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg, state_dir=state
        )
    finally:
        state.chmod(0o700)
    assert out == (2, UNRESOLVED + kiro.RETRY_HINT)
    recs = [r for r in _log_events(tmp_path, "kiro_guard")]
    assert [(r["status"], r["workspace"]) for r in recs] == [("ask", "personal")]
    [st] = _log_events(tmp_path, "kiro_guard_state")
    assert st["status"] == "error" and st["workspace"] == "personal"


def test_kiro_state_lock_held_elsewhere_still_blocks(cfg_tmp, guard_cfg, tmp_path):
    import fcntl

    state = tmp_path / "st"
    state.mkdir()
    fd = os.open(state / "kiro-guard.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.monotonic()
        out = _guard(
            cfg_tmp, _cwd(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg, state_dir=state
        )
        assert time.monotonic() - t0 < 2.0
    finally:
        os.close(fd)
    assert out == (2, UNRESOLVED + kiro.RETRY_HINT)
    assert not (state / "kiro-guard.json").exists()


def test_kiro_ask_is_recorded_under_the_state_lock(cfg_tmp, guard_cfg, tmp_path):
    state = tmp_path / "st"
    _guard(cfg_tmp, _cwd(cfg_tmp), FakeSession({"warnings": []}), gcfg=guard_cfg, state_dir=state)
    assert (state / "kiro-guard.lock").exists()
    assert len(json.loads((state / "kiro-guard.json").read_text())) == 1
