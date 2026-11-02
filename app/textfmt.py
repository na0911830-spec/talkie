"""Convert the model's light Markdown to Telegram-safe HTML."""
from __future__ import annotations

import html
import re

_CODE_BLOCK = re.compile(r"```(\w+)?\n?(.*?)```", re.S)


def md_to_telegram_html(text: str) -> str:
    blocks: list[str] = []

    def stash(match: re.Match) -> str:
        lang = match.group(1) or ""
        body = html.escape(match.group(2).rstrip("\n"), quote=False)
        cls = f' class="language-{lang}"' if lang else ""
        blocks.append(f"<pre><code{cls}>{body}</code></pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = _CODE_BLOCK.sub(stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Split long replies on paragraph/line boundaries, never inside a code fence."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for para in re.split(r"(\n\n)", text):
        if len(current) + len(para) > limit and current:
            parts.append(current)
            current = ""
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        current += para
    if current.strip():
        parts.append(current)
    # Re-balance code fences that got split.
    fixed: list[str] = []
    carry = False
    for part in parts:
        if carry:
            part = "```\n" + part
        carry = part.count("```") % 2 == 1
        if carry:
            part += "\n```"
        fixed.append(part)
    return fixed
