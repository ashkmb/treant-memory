"""The stale-fact check: flag repository facts whose code tokens are gone from ``HEAD``.

Reads the repository with ``git ls-tree HEAD`` paths and ``git grep -q`` / ``git log`` exit codes
and commit lines only, never file contents. Sends the server only tokens that appear verbatim
in the fact text, the ``HEAD`` commit id, and removing commit ids and subjects. Precision over
recall: any uncertain check (git error, timeout, submodule, no evidence for a weak token)
counts the token as present. Nothing here changes a fact; ``flag_stale`` only records a flag.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from treant_client import log
from treant_client.keychain import get_token
from treant_client.mcp_http import TreantError, TreantSession
from treant_client.stale_tokens import Token, checkable, extract_tokens
from treant_client.summarize import default_state_dir
from treant_client.workspaces import resolve

MAX_FACTS = 200
BUDGET_S = 30.0
LOG_MAX_COMMITS = 500
LOG_TIMEOUT_S = 5.0
GIT_TIMEOUT_S = 5.0
MEMO_FILE = "stale-heads.json"
MIN_HTTP_S = 1.0

_HEX = re.compile(r"[0-9a-f]{7,64}")
_PER_FACT_REFUSALS = ("invalid input", "not found")
# -C alone decides the repository; an inherited GIT_DIR or index would point elsewhere
_DROPPED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
_GLOB_SPECIAL = re.compile(r"([*?\[\]\\])")


def _git(top: str, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """One git call in ``top``; None when it could not run or timed out."""
    if timeout <= 0:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    try:
        return subprocess.run(
            ["git", "-C", top, *args], capture_output=True, timeout=timeout, check=False, env=env
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _suffixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _glob_escape(needle: str) -> str:
    return _GLOB_SPECIAL.sub(r"\\\1", needle)


def _stem(name: str) -> str:
    base, dot, _ = name.rpartition(".")
    return base if dot and base else name


def _is_weak(token: Token) -> bool:
    """A weak token is a common-looking name that may well never have been in the repo
    (``GitHub``, ``os.path``, ``Node.js``, ``README.md``): it counts as missing only when
    ``removed_in`` finds the commit that removed it. Weak: an identifier with a ``.``, or
    with no ``_`` and no ``()``; a path with no ``/``."""
    if token.kind == "ident":
        return "." in token.raw or ("_" not in token.raw and not token.raw.endswith("()"))
    return "/" not in token.needle


class RepoIndex:
    """Paths of one repository's ``HEAD`` commit (not the index or the work tree: the flag
    names ``HEAD``), and the git lookups for one run."""

    def __init__(self, top: str, deadline: float, clock=time.monotonic):
        self.top = top
        self.deadline = deadline
        self.clock = clock
        self.paths: set[str] = set()  # component suffixes of every tracked path and directory
        self.stems: set[str] = set()
        self.gitlinks: set[str] = set()  # component suffixes of every gitlink path
        self._idents: dict[str, bool] = {}
        self._range: str | None = None
        self.listed = False  # False: the file list is unknown, so every token counts as present

    def _left(self, cap: float) -> float:
        return min(cap, self.deadline - self.clock())

    @classmethod
    def load(cls, top: str, deadline: float, clock=time.monotonic) -> RepoIndex:
        idx = cls(top, deadline, clock)
        r = _git(top, ["ls-tree", "-r", "-z", "--full-tree", "HEAD"], idx._left(GIT_TIMEOUT_S))
        if r is None or r.returncode != 0:
            return idx
        idx.listed = True
        for entry in r.stdout.decode("utf-8", "replace").split("\0"):
            meta, tab, path = entry.partition("\t")  # "<mode> <type> <object>\t<path>"
            if not tab or not path:
                continue
            if meta.startswith("160000 "):  # a submodule's gitlink
                idx.gitlinks.update(_suffixes(path))
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                idx.paths.update(_suffixes("/".join(parts[:i])))
            idx.stems.update(_stem(p) for p in parts)
        return idx

    def has_path(self, needle: str) -> bool:
        if not self.listed or needle in self.paths:
            return True
        parts = needle.split("/")
        return any("/".join(parts[:i]) in self.gitlinks for i in range(1, len(parts) + 1))

    def _grep(self, needle: str) -> bool:
        r = _git(
            self.top,
            ["grep", "-F", "-w", "-q", "-e", needle, "HEAD", "--"],
            self._left(GIT_TIMEOUT_S),
        )
        return r is None or r.returncode != 1  # only a clean "no match" means absent

    def has_ident(self, needle: str) -> bool:
        if needle not in self._idents:
            last = needle.rsplit(".", 1)[-1]
            found = (
                not self.listed
                or needle in self.stems
                or self._grep(needle)
                or ("." in needle and (last in self.stems or self._grep(last)))
                or bool(self.gitlinks)  # the submodule may hold it; git grep cannot see in
            )
            self._idents[needle] = found
        return self._idents[needle]

    def _log_range(self) -> str | None:
        if self._range is None:
            r = _git(
                self.top,
                ["rev-list", "-1", f"--skip={LOG_MAX_COMMITS}", "HEAD"],
                self._left(GIT_TIMEOUT_S),
            )
            if r is None or r.returncode != 0:
                return None
            boundary = r.stdout.decode("ascii", "replace").strip()
            self._range = f"{boundary}..HEAD" if boundary else "HEAD"
        return self._range

    def removed_in(self, token: Token) -> tuple[str, str] | None:
        """(short commit, subject) of the latest commit among the last 500 that removed the
        token, or None when there is none or the lookup failed."""
        rng = self._log_range()
        if rng is None:
            return None
        # the owner's git config must not add signature checks or external diff programs
        args = ["log", "-1", "--no-show-signature", "--no-ext-diff", "--no-textconv"]
        args.append("--format=%h%x00%s")
        if token.kind == "path":
            needle = _glob_escape(token.needle)
            args += ["--diff-filter=DR", rng, "--"]
            # the second pathspec covers a directory token: the files under it
            args += [f":(glob)**/{needle}", f":(glob)**/{needle}/**"]
        else:
            args += [f"-S{token.needle}", rng]
        r = _git(self.top, args, self._left(LOG_TIMEOUT_S))
        if r is None or r.returncode != 0:
            return None
        line = r.stdout.decode("utf-8", "replace").strip("\n")
        short, nul, subject = line.partition("\0")
        if not nul or not _HEX.fullmatch(short):
            return None
        return short, subject.splitlines()[0] if subject else ""


@dataclass
class InDoubt:
    fact_id: str
    missing: list[dict]


@dataclass
class RunResult:
    status: str
    checked: int = 0
    flagged: int = 0
    in_doubt: list[InDoubt] = field(default_factory=list)


def _memo_path():
    return default_state_dir() / MEMO_FILE


def _memo_read() -> dict:
    try:
        data = json.loads(_memo_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _memo_write(key: str, head: str) -> None:
    # Read-merge-replace without a lock: two saves finishing together may drop each other's
    # entry. A lost entry only costs one more check after the next save.
    path = _memo_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = _memo_read()
    data[key] = head
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stale-heads-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _missing(idx: RepoIndex, tokens: list[Token]) -> list[dict]:
    out = []
    for token in tokens:
        present = (
            idx.has_path(token.needle) if token.kind == "path" else idx.has_ident(token.needle)
        )
        if present:
            continue
        evidence = idx.removed_in(token)
        if evidence is None and _is_weak(token):
            continue  # precision over recall: a weak token needs a removing commit
        commit, subject = evidence or (None, None)
        out.append({"token": token.raw, "commit": commit, "subject": subject or None})
    return out


def run(
    path: str,
    cfg,
    *,
    dry_run: bool,
    use_memo: bool,
    token_getter=get_token,
    session_factory=TreantSession,
    clock=time.monotonic,
    budget: float = BUDGET_S,
    http_timeout: float = 15.0,
) -> RunResult:
    """Check the facts of the repository holding ``path``. Never raises."""
    res = RunResult("failed_error")
    try:
        res.status = _run(
            res, path, cfg, dry_run, use_memo, token_getter, session_factory, clock, budget,
            http_timeout,
        )  # fmt: skip
    except TreantError as e:
        res.status = f"failed_{e.category}"
    except Exception:  # noqa: BLE001
        res.status = "failed_error"
    return res


def _run(
    res, path, cfg, dry_run, use_memo, token_getter, session_factory, clock, budget, http_timeout
) -> str:
    deadline = clock() + budget
    r = _git(path, ["rev-parse", "--show-toplevel"], min(GIT_TIMEOUT_S, deadline - clock()))
    top = r.stdout.decode("utf-8", "replace").strip() if r is not None and r.returncode == 0 else ""
    if not top:
        return "not_a_repo"
    r = _git(top, ["rev-parse", "--verify", "-q", "HEAD"], min(GIT_TIMEOUT_S, deadline - clock()))
    head = (
        r.stdout.decode("ascii", "replace").strip() if r is not None and r.returncode == 0 else ""
    )
    if not _HEX.fullmatch(head):
        return "no_head"
    ws = resolve(path, cfg)
    if not ws:
        return "skipped_unmapped"
    if cfg.url is None:
        return "skipped_no_url"
    repo = os.path.basename(top.rstrip("/"))
    key = os.path.realpath(top)
    if use_memo and _memo_read().get(key) == head:
        return "skipped_unchanged"
    token = token_getter(ws)
    if not token:
        return "skipped_no_token"
    terms = cfg.scrub_host_terms.get(ws, [])

    def http_left() -> float:
        # The budget is soft by up to one call: each HTTP call may take the time left, but
        # at least MIN_HTTP_S, so a call near the end is not cut off at once.
        return max(MIN_HTTP_S, min(http_timeout, deadline - clock()))

    status = "ok"
    with session_factory(cfg.url, token, http_left()) as sess:

        def call(name: str, args: dict):
            if hasattr(sess, "timeout"):
                sess.timeout = http_left()  # TreantSession reads it on every request
            return sess.call_tool(name, args)

        payload = call("repo_facts", {"repo": repo})
        facts = payload.get("facts") if isinstance(payload, dict) else None
        if not isinstance(facts, list):
            raise TreantError("protocol", "no facts")
        idx = RepoIndex.load(top, deadline, clock) if facts else None
        for fact in facts[:MAX_FACTS]:
            if clock() >= deadline:
                status = "stopped_budget"
                break
            if not isinstance(fact, dict):
                continue
            fid, typ, text = fact.get("id"), fact.get("type"), fact.get("text")
            if not (isinstance(fid, str) and isinstance(typ, str) and isinstance(text, str)):
                continue
            if not checkable(typ, text):
                continue
            res.checked += 1
            missing = _missing(idx, extract_tokens(text, terms))
            if not missing:
                continue
            res.in_doubt.append(InDoubt(fid, missing))
            if dry_run:
                continue
            args = {"fact_id": fid, "repo": repo, "commit": head, "missing": missing}
            try:
                out = call("flag_stale", args)
            except TreantError as e:
                if e.detail == "forbidden":
                    status = "skipped_forbidden"
                    break
                if e.detail in _PER_FACT_REFUSALS:
                    continue
                raise
            if isinstance(out, dict) and out.get("flagged") is True:
                res.flagged += 1
    if not dry_run and status in ("ok", "skipped_forbidden"):
        with contextlib.suppress(OSError):  # unremembered: the next save checks again
            _memo_write(key, head)
    return status


def check_after_save(cwd: str, cfg, *, token_getter, session_factory) -> str:
    """The stale check after a save, for the session's repository. Never raises."""
    t0 = time.monotonic()
    ws = None
    res = None
    with contextlib.suppress(Exception):
        ws = resolve(cwd, cfg)
    try:
        res = run(
            cwd,
            cfg,
            dry_run=False,
            use_memo=True,
            token_getter=token_getter,
            session_factory=session_factory,
        )
        status = res.status
    except Exception:  # noqa: BLE001
        status = "failed_error"
    log.event(
        "check_stale",
        workspace=ws,
        status=status,
        count=res.flagged if res is not None else 0,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return status
