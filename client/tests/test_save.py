import json
import os
import threading
import time

from treant_client.mcp_http import TreantError
from treant_client.save import save_worker

BIG = "USER: please refactor the module\nASSISTANT: done, see branch feat/x\n" * 40  # > 1500


class FakeSession:
    def __init__(self, result=None, raises=None, delay=0.0, record=None):
        self.result = result if result is not None else {"session_id": "s", "revision": 1}
        self.raises, self.delay, self.record = raises, delay, record
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
        time.sleep(self.delay)
        if self.record is not None:
            self.record.append(("end", time.monotonic()))
        if self.raises:
            raise self.raises
        return self.result


class FakeSummarizer:
    def __init__(self, result=None, delay=0.0, record=None):
        self.result = result if result is not None else {"journal": "- refactored module"}
        self.delay, self.record = delay, record
        self.inputs = []

    def __call__(self, text):
        if self.record is not None:
            self.record.append(("start", time.monotonic()))
        self.inputs.append(text)
        time.sleep(self.delay)
        return dict(self.result)


def tok(ws):
    return "tok-test"


def _cwd(cfg, suffix="Work"):
    return next(p for p in cfg.folders if p.endswith(suffix))


def _run(cfg, cwd, sess, summ=None, load=None, sid="sid-1", **kw):
    load = load or (lambda: (BIG, 3))
    kw.setdefault("token_getter", tok)
    return save_worker(
        sid,
        str(cwd),
        load,
        "session_end",
        cfg,
        session_factory=sess.factory,
        summarizer=summ or FakeSummarizer(),
        **kw,
    )


def _log(tmp_path):
    p = tmp_path / "_logs" / "client.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_saved_calls_save_session_with_session_id(cfg_tmp, tmp_path):
    s = FakeSession()
    summ = FakeSummarizer({"journal": "- did x", "facts": "- mail me at a.b@acme.com"})
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, summ) == "saved"
    assert s.token == "tok-test" and s.url == cfg_tmp.url
    [(name, args)] = s.calls
    assert name == "save_session"
    assert args["client_session_id"] == "sid-1"
    assert args["sections"] == {"journal": "- did x", "facts": "- mail me at [removed]"}
    assert summ.inputs == [BIG]
    [rec] = _log(tmp_path)
    assert rec["event"] == "save_worker" and rec["status"] == "saved"
    assert rec["workspace"] == "work" and rec["trigger"] == "session_end"
    assert "did x" not in json.dumps(rec)


def test_transcript_is_scrubbed_before_summarizing(cfg_tmp):
    summ = FakeSummarizer()
    text = BIG + "host build01.acme-dataplace.com and 10.1.2.3\n"
    assert _run(cfg_tmp, _cwd(cfg_tmp), FakeSession(), summ, load=lambda: (text, 3)) == "saved"
    assert "acme-dataplace" not in summ.inputs[0] and "10.1.2.3" not in summ.inputs[0]
    assert summ.inputs[0].count("[removed]") == 2


def test_unmapped_skips_without_token_or_load(cfg_tmp, tmp_path):
    called = []
    s = FakeSession()
    status = _run(
        cfg_tmp,
        tmp_path,
        s,
        load=lambda: called.append("load") or (BIG, 3),
        token_getter=lambda ws: called.append("token"),
    )
    assert status == "skipped_unmapped"
    assert called == [] and s.opened == 0
    assert not (tmp_path / "_locks").exists()


def test_no_token_skips(cfg_tmp):
    s = FakeSession()
    status = _run(cfg_tmp, _cwd(cfg_tmp), s, token_getter=lambda ws: None)
    assert status == "skipped_no_token" and s.opened == 0


def test_save_small_session_skipped(cfg_tmp):
    s, summ = FakeSession(), FakeSummarizer()
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, summ, load=lambda: (BIG, 1)) == "skipped_small"
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, summ, load=lambda: ("x" * 1499, 5)) == "skipped_small"
    assert s.opened == 0 and summ.inputs == []


def test_noisy_transcript_with_clean_summary_is_saved(cfg_tmp):
    s, summ = FakeSession(), FakeSummarizer()
    noisy = BIG + "".join(f"server 10.0.0.{i}\n" for i in range(13))
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, summ, load=lambda: (noisy, 3)) == "saved"
    assert "10.0.0." not in summ.inputs[0]
    assert summ.inputs[0].count("[removed]") == 13
    assert s.calls[0][1]["sections"] == {"journal": "- refactored module"}


def test_not_confident_sends_unsure(cfg_tmp):
    s = FakeSession()
    noisy = "".join(f"- 10.0.0.{i}\n" for i in range(13))
    summ = FakeSummarizer({"journal": "- ok", "facts": noisy})
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, summ) == "saved_unsure"
    args = s.calls[0][1]
    assert args["unsure"] is True
    assert "10.0.0." not in args["sections"]["facts"]


def test_confident_save_omits_unsure(cfg_tmp):
    s = FakeSession()
    assert _run(cfg_tmp, _cwd(cfg_tmp), s) == "saved"
    assert "unsure" not in s.calls[0][1]


def test_host_terms_are_per_workspace(cfg_tmp):
    summ = FakeSummarizer({"journal": "- touched build01.acme-dataplace.com"})
    s = FakeSession()
    assert _run(cfg_tmp, _cwd(cfg_tmp, "Volunteer"), s, summ) == "saved"
    assert "acme-dataplace" in s.calls[0][1]["sections"]["journal"]
    s2 = FakeSession()
    assert _run(cfg_tmp, _cwd(cfg_tmp), s2, summ) == "saved"
    assert s2.calls[0][1]["sections"]["journal"] == "- touched [removed]"


def test_empty_summary_skips(cfg_tmp):
    s = FakeSession()
    assert _run(cfg_tmp, _cwd(cfg_tmp), s, FakeSummarizer({})) == "skipped_empty_summary"
    assert s.opened == 0


def test_summarizer_run_failure_is_failed_summarize(cfg_tmp, tmp_path, monkeypatch):
    from test_summarize import LAUNCHD_PATH, launchd_run

    from treant_client.summarize import summarize

    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    s = FakeSession()
    status = _run(cfg_tmp, _cwd(cfg_tmp), s, summ=lambda t: summarize(t, run=launchd_run))
    assert status == "failed_summarize"
    assert s.opened == 0
    assert [e["status"] for e in _log(tmp_path) if e["event"] == "save_worker"] == [
        "failed_summarize"
    ]


def test_failures_map_to_categories(cfg_tmp, tmp_path):
    cwd = _cwd(cfg_tmp)
    assert _run(cfg_tmp, cwd, FakeSession(raises=TreantError("timeout"))) == "failed_timeout"
    assert _run(cfg_tmp, cwd, FakeSession(raises=TreantError("http_401"))) == "failed_http_401"
    assert _run(cfg_tmp, cwd, FakeSession(result={"nope": 1})) == "failed_protocol"

    def bad_load():
        raise OSError("gone")

    assert _run(cfg_tmp, cwd, FakeSession(), load=bad_load) == "failed_load"

    def boom(text):
        raise RuntimeError("TRANSCRIPT SECRET")

    assert _run(cfg_tmp, cwd, FakeSession(), summ=boom) == "failed_error"
    assert _run(cfg_tmp, cwd, FakeSession(), sid="") == "failed_invalid"
    assert _run(cfg_tmp, cwd, FakeSession(), sid="x" * 201) == "failed_invalid"
    assert "SECRET" not in (tmp_path / "_logs" / "client.log").read_text()


def test_config_error_is_failed_not_raised(monkeypatch, tmp_path):
    import treant_client.config as c

    def boom(*a, **k):
        raise ValueError("bad toml")

    monkeypatch.setattr(c, "load_config", boom)
    status = save_worker("sid", str(tmp_path), lambda: (BIG, 3), "session_end")
    assert status == "failed_config"


def test_lock_file_named_by_session_hash(cfg_tmp, tmp_path):
    import hashlib

    assert _run(cfg_tmp, _cwd(cfg_tmp), FakeSession()) == "saved"
    lock = tmp_path / "_locks" / (hashlib.sha256(b"sid-1").hexdigest() + ".lock")
    assert lock.exists()
    assert oct(os.stat(lock).st_mode & 0o777) == "0o600"


def test_concurrent_saves_serialize(cfg_tmp):
    record: list = []
    s = FakeSession(delay=0.2, record=record)
    results = []

    def worker():
        summ = FakeSummarizer(delay=0.2, record=record)
        results.append(_run(cfg_tmp, _cwd(cfg_tmp), s, summ))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == ["saved", "saved"]
    assert [kind for kind, _ in record] == ["start", "end", "start", "end"]
    assert record[1][1] <= record[2][1]
    assert len(s.calls) == 2


def test_lock_wait_is_bounded(cfg_tmp, tmp_path):
    import fcntl
    import hashlib
    import time

    d = tmp_path / "_locks"
    d.mkdir(mode=0o700)
    fd = os.open(d / (hashlib.sha256(b"sid-1").hexdigest() + ".lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # a stuck worker holds this session's lock
        t0 = time.monotonic()
        assert _run(cfg_tmp, _cwd(cfg_tmp), FakeSession(), lock_wait=0.3) == "failed_lock"
        assert time.monotonic() - t0 < 2.0
    finally:
        os.close(fd)
    assert _run(cfg_tmp, _cwd(cfg_tmp), FakeSession(), lock_wait=0.3) == "saved"
