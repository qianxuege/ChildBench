"""Small text helpers shared by agents (not agent-facing tools)."""

from __future__ import annotations

import re


def clean_child_line(raw: str, child_name: str) -> str:
    """Take a model's draft and keep only the child's spoken line."""
    text = (raw or "").strip()
    text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    line = lines[0]
    line = re.sub(rf"^(?:{re.escape(child_name)}|child|kid)\s*[:\-]\s*", "", line, flags=re.I)
    return line.strip().strip('"').strip("“”").strip()
