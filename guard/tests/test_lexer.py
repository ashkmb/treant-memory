import pytest

from treant_guard.lexer import (
    lex,
    option_values,
    parse,
    raw,
    strip_keywords,
    unwrap,
    words,
)


def _segments(command):
    """The command word lists of ``parse``."""
    return [value for what, value in parse(command) if what == "cmd"]


def test_segments_split_on_unquoted_separators():
    assert _segments("echo a;(kubectl get po) && x | y") == [
        ["echo", "a"],
        ["kubectl", "get", "po"],
        ["x"],
        ["y"],
    ]
    assert _segments("echo \"a;b\" 'c|d'") == [["echo", "a;b", "c|d"]]
    assert _segments("kubectl get po 2>&1 --context p") == [
        ["kubectl", "get", "po", "2>&1", "--context", "p"]
    ]
    assert _segments("kubectl get po \\\n --context p") == [
        ["kubectl", "get", "po", "--context", "p"]
    ]


def test_unbalanced_quote_raises():
    with pytest.raises(ValueError):
        lex('echo "x')


@pytest.mark.parametrize(
    "seg,expected",
    [
        (
            ["FOO=1", "env", "-u", "X", "BAR=2", "kubectl", "get"],
            ({"FOO": "1", "X": "", "BAR": "2"}, ["kubectl", "get"], False),
        ),
        (
            ["env", "--unset", "A", "--unset=B", "-i", "kubectl"],
            ({"A": "", "B": ""}, ["kubectl"], True),
        ),
        (["env", "-u", "A", "A=1", "aws"], ({"A": "1"}, ["aws"], False)),
        (["A=1", "env", "-uA", "aws"], ({"A": ""}, ["aws"], False)),
        (["sudo", "-u", "root", "aws"], ({}, ["aws"], False)),
        (["sudo", "-u", "root", "timeout", "60", "git", "push"], ({}, ["git", "push"], False)),
        (["timeout", "-s", "KILL", "-k", "5", "60", "kubectl"], ({}, ["kubectl"], False)),
        (["timeout", "--signal=TERM", "5m", "aws"], ({}, ["aws"], False)),
        (["exec", "-a", "name", "kubectl"], ({}, ["kubectl"], False)),
    ],
)
def test_unwrap(seg, expected):
    assert unwrap(seg) == expected


def test_option_values_forms():
    assert option_values(["--context", "a", "--context=b"], {"--context"}) == ["a", "b"]
    assert option_values(["-iinv/x", "-i", "y"], {"-i"}, short="-i") == ["inv/x", "y"]


def test_words():
    assert words("kubectl --context=Prod-EU get po") == [
        "--context",
        "get",
        "kubectl",
        "po",
        "prod-eu",
    ]
    assert words('echo "x') == ['"x', "echo"]


def _kinds(command):
    return [v for k, v in parse(command) if k == "sep"]


def test_parse_separator_kinds():
    assert parse("a && b; c || d | e & f") == [
        ("cmd", ["a"]),
        ("sep", "and"),
        ("cmd", ["b"]),
        ("sep", "seq"),
        ("cmd", ["c"]),
        ("sep", "or"),
        ("cmd", ["d"]),
        ("sep", "pipe"),
        ("cmd", ["e"]),
        ("sep", "bg"),
        ("cmd", ["f"]),
    ]
    assert _kinds("a |& b\nc") == ["pipe", "seq"]
    assert _kinds("a &&\n b") == ["and"]
    assert _kinds("case x in a) b ;; esac") == ["close", "seq"]


def test_parse_groups():
    assert parse("(cd x; y) && z") == [
        ("sep", "open"),
        ("cmd", ["cd", "x"]),
        ("sep", "seq"),
        ("cmd", ["y"]),
        ("sep", "close"),
        ("sep", "and"),
        ("cmd", ["z"]),
    ]


def test_parse_redirects_are_not_separators():
    assert parse("a 2>&1 | b") == [
        ("cmd", ["a", "2>&1"]),
        ("sep", "pipe"),
        ("cmd", ["b"]),
    ]
    assert parse("a &> f; b") == [("cmd", ["a", "&>", "f"]), ("sep", "seq"), ("cmd", ["b"])]
    assert parse("a >& f") == [("cmd", ["a", ">&", "f"])]


def test_parse_quotes_keep_separators():
    assert parse('echo "a;b" && c') == [
        ("cmd", ["echo", "a;b"]),
        ("sep", "and"),
        ("cmd", ["c"]),
    ]
    with pytest.raises(ValueError):
        parse('echo "x')


@pytest.mark.parametrize(
    "seg,expected",
    [
        (["then", "kubectl", "get"], (None, ["kubectl", "get"])),
        (["if", "!", "kubectl", "get"], (None, ["kubectl", "get"])),
        (["elif", "{", "aws", "s3"], (None, ["aws", "s3"])),
        (["do", "kubectl"], (None, ["kubectl"])),
        (["while", "kubectl", "get"], ("while", ["kubectl", "get"])),
        (["until", "!", "x"], ("until", ["x"])),
        (["for", "x", "in", "a"], ("for", [])),
        (["select", "x", "in", "a"], ("select", [])),
        (["do", "for", "x", "in", "a"], ("for", [])),
        (["case", "x", "in"], ("case", [])),
        (["esac"], (None, [])),
        (["fi"], (None, [])),
        (["kubectl", "then"], (None, ["kubectl", "then"])),
    ],
)
def test_strip_keywords(seg, expected):
    assert strip_keywords(seg) == expected


@pytest.mark.parametrize(
    "seg",
    [
        ["env", "-i", "aws"],
        ["env", "-", "aws"],
        ["env", "--ignore-environment", "A=b", "aws"],
    ],
)
def test_unwrap_env_clear(seg):
    assigns, argv, clear = unwrap(seg)
    assert clear is True
    assert argv == ["aws"]
    assert assigns == ({"A": "b"} if "A=b" in seg else {})


@pytest.mark.parametrize(
    "command,kind",
    [("a ||\n b", "or"), ("a |\n b", "pipe"), ("a &&\n b", "and")],
)
def test_parse_newline_continuation_keeps_kind(command, kind):
    assert parse(command) == [("cmd", ["a"]), ("sep", kind), ("cmd", ["b"])]


def test_and_is_a_separator_for_segments():
    assert _segments("a && b") == [["a"], ["b"]]
    assert _kinds("a && b && c || d") == ["and", "and", "or"]


def test_hash_and_continuation_tokens():
    assert lex("echo a#b '#' \"#\" \\# ${#x}") == ["echo", "a#b", "#", "#", "#", "${#x}"]
    assert _segments("echo 'a # b'; c") == [["echo", "a # b"], ["c"]]
    assert lex('echo "a\\\nb"') == ["echo", "ab"]


def test_unwrap_zsh_precommand_modifiers_and_append():
    """zsh `noglob`, `nocorrect`, `-` run the next command; `A+=b` is an assignment
    whose value depends on the old one."""
    for wrapper in ("noglob", "nocorrect", "-"):
        assert unwrap([wrapper, "kubectl", "get"]) == ({}, ["kubectl", "get"], False)
    assert unwrap(["A+=b", "aws"]) == ({"A": "${A}b"}, ["aws"], False)
    assert unwrap(["env", "A+=b", "aws"]) == ({"A": "${A}b"}, ["aws"], False)


@pytest.mark.parametrize(
    "seg,expected",
    [
        (["repeat", "3", "kubectl", "get"], ("repeat", ["kubectl", "get"])),
        (["repeat", "3", "do", "kubectl"], ("repeat", ["kubectl"])),
        (["foreach", "x"], ("foreach", [])),
    ],
)
def test_strip_zsh_loop_keywords(seg, expected):
    assert strip_keywords(seg) == expected


def test_parse_keeps_how_each_word_was_written():
    """The resolver needs the written form (quotes, backslashes) of each word."""
    items = parse("a \"b c\" \\d e\"\"f '$X' ''; g")
    assert items == [("cmd", ["a", "b c", "d", "ef", "$X", ""]), ("sep", "seq"), ("cmd", ["g"])]
    assert [raw(w) for w in items[0][1]] == ["a", '"b c"', "\\d", 'e""f', "'$X'", "''"]
    assert raw("plain") == "plain"
    _, argv, _ = unwrap(strip_keywords(items[0][1])[1])
    assert raw(argv[1]) == '"b c"'


def test_case_fall_through_terminators_are_one_separator():
    """bash 4+ `;&` and `;;&`, zsh `;&` and `;|` end a `case` item; bash 3.2 (and zsh for
    `;;&`) reject them. `;&>file` is `;` and a redirect (bash 3.2 runs it)."""
    for term in (";&", ";;&", ";|", ";;"):
        command = f"case a in a) echo one{term} b) echo two;; esac"
        assert parse(command) == [
            ("cmd", ["case", "a", "in", "a"]),
            ("sep", "close"),
            ("cmd", ["echo", "one"]),
            ("sep", "seq"),
            ("cmd", ["b"]),
            ("sep", "close"),
            ("cmd", ["echo", "two"]),
            ("sep", "seq"),
            ("cmd", ["esac"]),
        ], term
    assert parse("echo a;&>/dev/null echo b") == [
        ("cmd", ["echo", "a"]),
        ("sep", "seq"),
        ("cmd", ["&>/dev/null", "echo", "b"]),
    ]


def test_separator_sentinel_in_the_input_is_dropped():
    """The NUL the lexer marks separators with can't fake or hide one."""
    assert parse("kubectl --con\x00text prod get po") == [
        ("cmd", ["kubectl", "--context", "prod", "get", "po"])
    ]
    assert parse("a \x00; b") == [("cmd", ["a"]), ("sep", "seq"), ("cmd", ["b"])]
    assert parse('a "x\x00;y"') == [("cmd", ["a", "x;y"])]


def test_backslash_escapes():
    assert parse('kubectl --context "pr\\"od" get') == [
        ("cmd", ["kubectl", "--context", 'pr"od', "get"])
    ]
    assert parse("kubectl --context 'a\\b' get") == [
        ("cmd", ["kubectl", "--context", "a\\b", "get"])
    ]
    assert parse(r"kubectl --context a\;b get") == [("cmd", ["kubectl", "--context", "a;b", "get"])]
    assert parse('kubectl --context "a\\\\" get; ls') == [
        ("cmd", ["kubectl", "--context", "a\\", "get"]),
        ("sep", "seq"),
        ("cmd", ["ls"]),
    ]


def test_option_values_edge_forms():
    assert option_values(["--context"], {"--context"}) == []  # no value after it
    assert option_values(["--context=", "-i"], {"--context"}, short="-i") == [""]
    assert option_values(["--inventory", "x", "-i"], {"--inventory"}, short="-i") == ["x"]
