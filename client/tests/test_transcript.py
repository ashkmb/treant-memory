import json

from treant_client.transcript import claude_text, kiro_text


def _write(path, lines):
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    return path


def _user(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def _assistant(content):
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def test_claude_text_roles_and_tools(tmp_path):
    p = _write(
        tmp_path / "t.jsonl",
        [
            _user("fix the build"),
            _assistant(
                [
                    {"type": "thinking", "thinking": "SECRET THOUGHTS"},
                    {"type": "text", "text": "Looking at it."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "make"}},
                ]
            ),
            _user([{"type": "tool_result", "tool_use_id": "x", "content": "TOOL OUTPUT"}]),
            _user([{"type": "text", "text": "thanks"}]),
        ],
    )
    text, turns = claude_text(p)
    assert text == "USER: fix the build\nASSISTANT: Looking at it.\n[tool Bash]\nUSER: thanks"
    assert turns == 2
    assert "SECRET THOUGHTS" not in text and "TOOL OUTPUT" not in text and "make" not in text


def test_unknown_and_invalid_lines_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    good = json.dumps(_user("hello")).encode()
    junk = [
        b"not json at all",
        b"\xff\xfe\x00binary\x00junk",
        b"[1, 2, 3]",
        b"42",
        json.dumps({"type": "summary", "summary": "compact summary"}).encode(),
        json.dumps({"type": "system", "content": "sys"}).encode(),
        json.dumps({"type": "user"}).encode(),
        json.dumps({"type": "user", "message": "str"}).encode(),
        json.dumps({"type": "user", "message": {"content": 7}}).encode(),
        json.dumps(
            _assistant([1, None, "x", {"type": "text", "text": 5}, {"type": "tool_use"}])
        ).encode(),
        json.dumps(_user("caveat", isMeta=True)).encode(),
        b"",
        b'{"type": "user", "message": {"content": "trunc',
    ]
    p.write_bytes(b"\n".join([good, *junk, good]) + b"\n")
    text, turns = claude_text(p)
    assert text == "USER: hello\n[tool ?]\nUSER: hello"
    assert turns == 2


def test_claude_text_missing_file_raises_oserror(tmp_path):
    import pytest

    with pytest.raises(OSError):
        claude_text(tmp_path / "missing.jsonl")


def test_large_transcript_head_tail(tmp_path):
    p = tmp_path / "big.jsonl"
    line = json.dumps(_user("x" * 990)) + "\n"
    with open(p, "w") as fh:
        fh.write(json.dumps(_user("FIRST")) + "\n")
        fh.writelines(line for _ in range(5_000))
        fh.write(json.dumps(_assistant([{"type": "text", "text": "LAST"}])) + "\n")
    assert p.stat().st_size >= 5_000_000
    text, turns = claude_text(p)
    assert len(text) <= 400_010
    assert text.startswith("USER: FIRST")
    assert text.endswith("ASSISTANT: LAST")
    assert "\n[…]\n" in text
    assert turns == 5_001


def test_head_tail_exact_bounds(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("a" * 100)])
    text, _ = claude_text(p, head=10, tail=20)
    assert text == "USER: aaaa" + "\n[…]\n" + "a" * 20
    short, _ = claude_text(p, head=1_000, tail=0)
    assert short == "USER: " + "a" * 100
    tail_only, _ = claude_text(p, head=10, tail=0)
    assert tail_only == "USER: aaaa\n[…]\n"


def test_kiro_text():
    value = {
        "transcript": ["> how do I deploy", "Use the runbook.", 5, None, "> ok"],
        "history": [{"user": 1}, {"user": 2}, {"user": 3}],
    }
    text, turns = kiro_text(value)
    assert text == "> how do I deploy\nUse the runbook.\n> ok"
    assert turns == 3
    big = {"transcript": ["y" * 500_000], "history": []}
    t, n = kiro_text(big, head=100, tail=200)
    assert len(t) == 100 + len("\n[…]\n") + 200 and n == 0
    assert kiro_text({}) == ("", 0)
    assert kiro_text({"transcript": "str", "history": "x"}) == ("", 0)
    assert kiro_text("not a dict") == ("", 0)


def test_claude_text_skips_deeply_nested_line(tmp_path):
    p = tmp_path / "t.jsonl"
    ok = {"type": "user", "message": {"content": "after the bomb"}}
    p.write_text("[" * 100_000 + "]" * 100_000 + "\n" + json.dumps(ok) + "\n")
    text, turns = claude_text(p)
    assert text == "USER: after the bomb" and turns == 1


def test_bounded_join_matches_clip_of_the_full_join():
    import random

    from treant_client.transcript import _BoundedJoin, clip

    rnd = random.Random(7)
    for head, tail in ((5, 7), (10, 0), (0, 9), (3, 3), (50, 40)):
        for _ in range(200):
            parts = ["x" * rnd.randrange(0, 30) + str(i) for i in range(rnd.randrange(0, 40))]
            j = _BoundedJoin(head, tail)
            for p in parts:
                j.add(p)
            assert j.text() == clip("\n".join(parts), head, tail), (head, tail, parts)


def test_bounded_join_keeps_memory_bounded():
    from treant_client.transcript import _BoundedJoin

    j = _BoundedJoin(10, 10)
    for i in range(10_000):
        j.add(f"part {i} " + "y" * 100)
        assert j.held() <= 8 * (10 + 10) + 200
    assert j.text().endswith("y" * 10) and j.text().startswith("part 0 yy")


def test_claude_text_holds_only_head_and_tail(tmp_path, monkeypatch):
    from treant_client import transcript

    sizes = []
    real = transcript._BoundedJoin.add

    def spy(self, part):
        real(self, part)
        sizes.append(self.held())

    monkeypatch.setattr(transcript._BoundedJoin, "add", spy)
    p = tmp_path / "t.jsonl"
    _write(p, [_user("u" * 1000) for _ in range(500)])
    _, turns = transcript.claude_text(p, head=100, tail=100)
    assert turns == 500 and max(sizes) < 5000
