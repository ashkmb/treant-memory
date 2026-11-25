import json
import time

import pytest

from treant import gate_rules as g
from treant.errors import InvalidInput
from treant.gate_rules import DenyList, Finding, check

EMPTY = DenyList()


def spans(text, deny=EMPTY, category=None):
    return [
        (f.category, text[f.start : f.end])
        for f in check(text, deny)
        if not category or f.category == category
    ]


def test_ticket_generic_and_prefix():
    assert spans("see ABC-1234 now", category="ticket_id") == [("ticket_id", "ABC-1234")]
    assert spans("see abc-12", category="ticket_id") == []
    d = DenyList(ticket_prefixes=("proj", "ab-"))
    assert spans("fix proj-77 and PROJ88 and ab-5", d, "ticket_id") == [
        ("ticket_id", "proj-77"),
        ("ticket_id", "PROJ88"),
        ("ticket_id", "ab-5"),
    ]


def test_ip_addresses():
    assert spans("host 10.1.2.3, and 2001:db8::1 ok", category="ip_address") == [
        ("ip_address", "10.1.2.3"),
        ("ip_address", "2001:db8::1"),
    ]
    assert spans("version 1.2.3", category="ip_address") == []


def test_account_id():
    assert spans("acct 123456789012.", category="account_id") == [("account_id", "123456789012")]
    assert spans("1234567890123", category="account_id") == []


def test_domain_and_subdomains():
    d = DenyList(domains=("acme.io",))
    assert spans("go to api.acme.io/x or ACME.io", d, "domain") == [
        ("domain", "api.acme.io"),
        ("domain", "ACME.io"),
    ]
    assert spans("notacme.io", d, "domain") == []
    assert spans("acme.iox", d, "domain") == []


def test_private_host():
    assert spans("ssh db1.corp.internal now", category="private_host") == [
        ("private_host", "db1.corp.internal")
    ]
    assert spans("printer.home.arpa", category="private_host") == [
        ("private_host", "printer.home.arpa")
    ]
    assert spans("local", category="private_host") == []


def test_instance_name_boundaries_and_case():
    d = DenyList(names=frozenset({"blue-eks", "blue"}))
    assert spans("use Blue-EKS here", d, "instance_name") == [
        ("instance_name", "Blue"),
        ("instance_name", "Blue-EKS"),
    ]
    assert spans("blue-eks-2 and xblue", d, "instance_name") == [
        ("instance_name", "blue"),
        ("instance_name", "blue-eks"),
    ]
    assert spans("the blue.", d, "instance_name") == [("instance_name", "blue")]


def test_term_substring_inside_word():
    d = DenyList(terms=("acme",))
    assert spans("the AcmeCorp team, acme_x", d, "term") == [
        ("term", "AcmeCorp"),
        ("term", "acme"),
    ]


def test_short_names_ignored():
    d = DenyList(names=frozenset({"ab", "x1"}))
    assert check("ab x1 ab", d) == []


def test_findings_sorted_overlaps_kept_dedup():
    d = DenyList(names=frozenset({"abc-1"}), ticket_prefixes=("ABC",))
    out = check("ABC-1 and ABC-1", d)
    cats = [(f.category, f.start, f.end) for f in out]
    assert cats == sorted(cats, key=lambda c: (c[1], c[2], c[0]))
    assert len(out) == len(set(out))
    assert {f.category for f in out} == {"ticket_id", "instance_name"}
    assert sum(f.category == "ticket_id" for f in out) == 2


def test_reason_never_contains_name():
    d = DenyList(names=frozenset({"secretcluster"}), terms=("zzq",))
    out = check("secretcluster and zzqword", d)
    assert out
    dumped = json.dumps([f.__dict__ for f in out])
    assert "secret" not in dumped and "zzq" not in dumped
    assert Finding("instance_name", 0, 13) in out


def test_rules_linear_time():
    d = DenyList(
        names=frozenset({"blue-eks"}),
        domains=("acme.io",),
        ticket_prefixes=("PRJ",),
        terms=("acme",),
    )
    for chunk in ["a" * 200_000, "a.b-" * 50_000, "acme" * 50_000, "1:" * 100_000, "a-1 " * 50_000]:
        t = time.perf_counter()
        check(chunk, d)
        assert time.perf_counter() - t < 0.2


def test_gate_rules_block_other_workspace_instance_name(db):
    with db.ws_tx("beta") as c:
        c.execute(
            "insert into ws_beta.nodes (kind, type, name, aliases)"
            " values ('instance', 'cluster', 'blue-eks', array['bluealias'])"
        )
    deny = g.load_deny(db)
    assert {"blue-eks", "bluealias"} <= deny.names
    # text from workspace alpha naming beta's instance
    found = check("deploy to blue-eks tonight", deny)
    assert [f.category for f in found] == ["instance_name"]


def test_load_deny_unions_settings_and_fails_closed(fresh, fresh_db):
    with fresh_db.admin_tx() as c:
        c.execute(
            "update control.workspaces set settings = %s::jsonb where name = 'alpha'",
            ('{"rules": {"domains": ["a.io"], "terms": ["tt1"], "ticket_prefixes": ["AA"]}}',),
        )
    deny = g.load_deny(fresh_db)
    assert (deny.domains, deny.terms, deny.ticket_prefixes) == (("a.io",), ("tt1",), ("AA",))
    with fresh_db.admin_tx() as c:
        c.execute("update control.workspaces set settings = '{\"bogus\": 1}' where name = 'gamma'")
    with pytest.raises(InvalidInput):
        g.load_deny(fresh_db)


@pytest.mark.parametrize(
    "text",
    [
        "prod-blue-eks",
        "blue-eks-1",
        "blue_eks",
        "blue eks",
        "blue\u2013eks",
        "blue.eks",
        "blue/eks",
        "\uff42\uff4c\uff55\uff45\uff0d\uff45\uff4b\uff53",
        "bl\u200bue-e\u00adks",
        "BLUE   \n  EKS",
        "blue--eks",
    ],
)
def test_instance_name_variants_found(text):
    d = DenyList(names=frozenset({"blue-eks"}))
    out = check(text, d)
    assert [f.category for f in out] == ["instance_name"]
    assert out[0].end > out[0].start


def test_single_token_name_only_whole_token():
    d = DenyList(names=frozenset({"eks"}))
    assert check("eksctl and myeks", d) == []
    assert spans("run EKS now", d, "instance_name") == [("instance_name", "EKS")]


def test_span_covers_original_characters():
    d = DenyList(names=frozenset({"blue-eks"}))
    t = "x (bl\u200bue_ eks) y"
    (f,) = check(t, d)
    assert t[f.start : f.end] == "bl\u200bue_ eks"


def test_other_punctuation_breaks_name():
    d = DenyList(names=frozenset({"blue-eks"}))
    assert check("blue, eks", d) == []


def test_term_variants_and_unique_tokens():
    d = DenyList(terms=("Acme",))
    t = "\uff21cme-corp ACMEx acmeacme acm"
    assert [t[f.start : f.end] for f in check(t, d)] == ["\uff21cme", "ACMEx", "acmeacme"]


def test_domain_variants():
    d = DenyList(domains=("Acme.IO",))
    t = "git.acme\u2024io acme\uff0eio:443 x.acme.io.evil.com"
    out = [t[f.start : f.end] for f in check(t, d) if f.category == "domain"]
    assert out == ["git.acme\u2024io", "acme\uff0eio", "x.acme.io"]


def test_prefix_lookup_is_by_set():
    d = DenyList(ticket_prefixes=("Proj", "my-app-"))
    assert spans("PROJ-5 proj9 my-app-12 MY_APP12 other-5", d, "ticket_id") == [
        ("ticket_id", "PROJ-5"),
        ("ticket_id", "proj9"),
        ("ticket_id", "my-app-12"),
        ("ticket_id", "MY_APP12"),
    ]


def test_rules_perf_large_deny_lists():
    import random

    rnd = random.Random(1)
    words = [
        "".join(rnd.choice("abcdefghijklmnop") for _ in range(rnd.randint(3, 9)))
        for _ in range(20000)
    ]
    names = frozenset(f"{rnd.choice(words)}-{rnd.choice(words)}-{i}" for i in range(5000))
    d = DenyList(
        names=names,
        domains=tuple(f"{rnd.choice(words)}{i}.example{i}.io" for i in range(200)),
        ticket_prefixes=tuple(f"PX{i}Q" for i in range(200)),
        terms=tuple(f"zz{rnd.choice(words)}{i}" for i in range(200)),
    )
    text = " ".join(rnd.choice(words) for _ in range(30000))[:200_000]
    check("warm up", d)
    t = time.perf_counter()
    check(text, d)
    assert time.perf_counter() - t < 0.5
