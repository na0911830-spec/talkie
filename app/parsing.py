"""Pure helpers (standard library only): model output parsing and text utilities."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""


def decode_arguments(raw: Any) -> dict:
    """Tool arguments arrive as dicts, JSON strings, or double-encoded JSON."""
    value = raw
    for _ in range(3):
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                value = json.loads(text)
                continue
            except json.JSONDecodeError:
                return {"input": text}
        break
    return value if isinstance(value, dict) else {}


_TEXT_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_text_tool_calls(content: str, known: set[str]) -> tuple[str, list[ToolCall]]:
    """Some small models write tool calls as text. Recover them."""
    calls: list[ToolCall] = []
    candidates = _TEXT_TOOL_RE.findall(content or "")
    stripped = (content or "").strip()
    if not candidates and stripped.startswith("{") and stripped.endswith("}"):
        candidates = [stripped]
    for blob in candidates:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        name = data.get("name") or data.get("tool") or (data.get("function") or {}).get("name")
        if name in known:
            args = data.get("arguments", data.get("parameters", data.get("args", {})))
            calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=decode_arguments(args)))
    if calls:
        content = _TEXT_TOOL_RE.sub("", content).strip()
        if content.startswith("{") and content.endswith("}"):
            content = ""
    return content, calls


def parse_chat_response(result: dict, known_tools: set[str]) -> ChatResult:
    """Normalise both Workers AI response shapes (OpenAI-style and legacy)."""
    if "choices" in result:
        msg = (result.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
    else:
        content = result.get("response") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        raw_calls = result.get("tool_calls") or []
    calls: list[ToolCall] = []
    for rc in raw_calls:
        fn = rc.get("function") or rc
        name = fn.get("name")
        if not name:
            continue
        calls.append(ToolCall(
            id=rc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            name=name,
            arguments=decode_arguments(fn.get("arguments", {})),
        ))
    if not calls and known_tools:
        content, calls = parse_text_tool_calls(content, known_tools)
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return ChatResult(content=content, tool_calls=calls)


# --- conversation text helpers ------------------------------------------------
_HINGLISH = {"hai", "hain", "yaar", "kya", "main", "mujhe", "mera", "meri", "nahi", "nahin", "kaise", "hoon",
             "bahut", "aaj", "kar", "karo", "kuch", "bata", "batao", "tum", "tera", "accha", "acha", "bhai",
             "haan", "kyun", "kab", "kaun", "raha", "rahi", "gaya", "hum", "tujhe", "chal", "abhi", "sab"}


def language_hint(text: str) -> str:
    """A per-turn reminder that keeps small models from switching script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "Reply in the language of the conversation."
    deva = sum("\u0900" <= c <= "\u097f" for c in letters) / len(letters)
    if deva > 0.3:
        return "The user wrote in Hindi (Devanagari script). Reply in Hindi using Devanagari."
    latin = sum(c.isascii() for c in letters) / len(letters)
    words = set(re.findall(r"[a-z]+", text.lower()))
    if latin > 0.9 and len(words & _HINGLISH) >= 2:
        return ("The user wrote in Hinglish (Hindi typed in English letters). Reply in Hinglish with Latin "
                "letters only; do NOT use Devanagari.")
    if latin > 0.9:
        return "The user wrote in English (or another Latin-script language). Reply in that same language."
    return "Reply in exactly the same language and script as the user's last message."


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def clean_answer(text: str, had_media: bool) -> str:
    """Remove markdown images (media is delivered separately) and invented media links."""
    text = _MD_IMAGE.sub("", text)
    if had_media:
        text = re.sub(r"^.*\b(Preview URL|Artwork|Image URL|Audio URL)\b.*$", "", text, flags=re.M | re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def history_to_messages(rows: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    for r in rows:
        role = "assistant" if r["role"] == "assistant" else "user"
        content = r["content"]
        atts = r.get("attachments") or []
        if atts:
            content += "\n[sent: " + ", ".join(a.get("type", "file") for a in atts) + "]"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    return msgs


def parse_fact_list(text: str) -> list[str]:
    match = re.search(r"\[.*\]", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [str(x).strip() for x in data if isinstance(x, str) and 3 <= len(x.strip()) <= 200][:6]
