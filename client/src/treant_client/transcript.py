"""Turn a session record into bounded plain text for the summarizer.

Claude Code's transcript JSONL is internal and changes between versions, so parsing is
defensive: anything that is not a recognised user or assistant line is skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

# Opens the briefing `treant-client kiro-brief` hands a resumed Kiro chat as its first message.
# The sweep drops such messages, so the briefing never flows back into memory.
RESUME_MARKER = "[treant: resume briefing]"

HEAD = 50_000
TAIL = 350_000
CUT = "\n[…]\n"


def clip(text: str, head: int = HEAD, tail: int = TAIL) -> str:
    """Keep the first ``head`` and last ``tail`` characters of an over-long text."""
    if len(text) <= head + tail:
        return text
    return text[:head] + CUT + (text[len(text) - tail :] if tail > 0 else "")


class _BoundedJoin:
    """``clip("\n".join(parts), head, tail)`` built part by part, holding about 2 * (head + tail)
    characters at most instead of the whole session."""

    def __init__(self, head: int, tail: int):
        self.head, self.tail = head, tail
        self._first: str | None = None  # the head, once the text is known to be clipped
        self._parts: list[str] = []
        self._size = 0  # characters in "\n".join(self._parts)

    def add(self, part: str) -> None:
        self._size += len(part) + (1 if self._parts else 0)
        self._parts.append(part)
        if self._size > 2 * (self.head + self.tail):
            joined = "\n".join(self._parts)
            if self._first is None:  # longer than head + tail: it will be clipped
                self._first, joined = joined[: self.head], joined[self.head :]
            kept = joined[len(joined) - self.tail :] if self.tail > 0 else ""
            self._parts, self._size = [kept], len(kept)

    def held(self) -> int:
        return len(self._first or "") + self._size

    def text(self) -> str:
        joined = "\n".join(self._parts)
        if self._first is None:
            return clip(joined, self.head, self.tail)
        return self._first + CUT + (joined[len(joined) - self.tail :] if self.tail > 0 else "")


def _line_parts(record: object) -> tuple[list[str], bool]:
    """(text parts, counts as a user turn) for one parsed JSONL line."""
    if not isinstance(record, dict):
        return [], False
    role = record.get("type")
    if role not in ("user", "assistant") or record.get("isMeta") is True:
        return [], False
    message = record.get("message")
    if not isinstance(message, dict):
        return [], False
    label = "USER: " if role == "user" else "ASSISTANT: "
    content = message.get("content")
    if isinstance(content, str):
        blocks: list = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        return [], False
    parts: list[str] = []
    said = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(label + text)
                said = True
        elif kind == "tool_use":
            name = block.get("name")
            parts.append(f"[tool {name if isinstance(name, str) and name else '?'}]")
    return parts, role == "user" and said


def claude_text(path: Path, head: int = HEAD, tail: int = TAIL) -> tuple[str, int]:
    """(text, user_turns) from a Claude Code transcript. Raises OSError if unreadable."""
    parts = _BoundedJoin(head, tail)
    turns = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):  # RecursionError: a deeply nested line
                continue
            got, is_turn = _line_parts(record)
            for part in got:
                parts.add(part)
            turns += is_turn
    return parts.text(), turns


def kiro_text(value: dict, head: int = HEAD, tail: int = TAIL) -> tuple[str, int]:
    """(text, user_turns) from a Kiro conversation value (``transcript`` + ``history``)."""
    if not isinstance(value, dict):
        return "", 0
    transcript = value.get("transcript")
    history = value.get("history")
    lines = [t for t in transcript if isinstance(t, str)] if isinstance(transcript, list) else []
    turns = len(history) if isinstance(history, list) else 0
    return clip("\n".join(lines), head, tail), turns


def kiro_v3_text(path: Path, head: int = HEAD, tail: int = TAIL) -> tuple[str, int]:
    """(text, user_turns) from a Kiro v3 session's ``messages.jsonl``: the user's chat messages,
    the assistant's replies and the names of the tools it called. Steering text Kiro injects as a
    user message, reasoning and tool output are left out. A session with no messages file (opened
    and closed without a message) is empty. Raises OSError if unreadable."""
    parts = _BoundedJoin(head, tail)
    turns = 0
    if not Path(path).exists():
        return "", 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            kind, content = payload.get("type"), payload.get("content")
            if kind == "user" and payload.get("source") != "steer":
                if (
                    isinstance(content, str)
                    and content.strip()
                    and not content.startswith(RESUME_MARKER)
                ):
                    parts.add("USER: " + content)
                    turns += 1
            elif kind == "assistant" and payload.get("operationType") == "Say":
                if isinstance(content, str) and content.strip():
                    parts.add("ASSISTANT: " + content)
            elif kind == "tool_call":
                name = payload.get("toolName")
                parts.add(f"[tool {name if isinstance(name, str) and name else '?'}]")
    return parts.text(), turns
