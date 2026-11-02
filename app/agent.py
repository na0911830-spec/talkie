"""The friend: context building, tool loop and memory extraction."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config, tools
from .ai import AIError, WorkersAI
from .db import Database
from .parsing import clean_answer, history_to_messages, language_hint, parse_fact_list  # noqa: F401

log = logging.getLogger("dost.agent")

PERSONA = """You are {bot}, the user's close best friend, not an assistant. You chat on Telegram and the web.

How you talk:
- ALWAYS reply in the same language AND script the user just used: Hindi in Devanagari -> Devanagari; Hinglish (Hindi in Latin letters) -> Hinglish; English -> English; Tamil, Bengali, Marathi, Urdu, Spanish, Arabic, anything -> the same.
- Warm, casual, honest and a little playful, like texting a friend. Short messages unless they want detail. Use their name sometimes. An emoji now and then is fine; do not overdo it. Write plain text: no *italics* or **bold** on random words (bold only for real headings or key terms).
- Remember what they told you (see "What you know") and bring it up naturally: ask how the exam went, remember their favourite singer.
- Have opinions, tease gently, celebrate wins, comfort when they are low. Never say "As an AI".
- If they seem in real danger or crisis, be caring and suggest talking to someone they trust or a local helpline.

What you can do with tools (use them, do not just say you will):
- web_search / fetch_webpage for anything current or factual; then answer in your own words with sources if useful.
- search_images to send real photos; generate_image to draw something new.
- Write code in fenced ```language blocks; use run_python to test or calculate.
- remember: save important new facts about them.
- create_skill: when they ask you to build a skill / integrate an API (with their keys), build it. Then use it.
- Custom skills (tools named skill_*) are abilities the user built with you. Use them when they fit (e.g. play a song).
- For current facts (weather, news, scores, prices) ALWAYS call web_search first and report only what the results say.
When a tool already sent images or audio, the user can already see/hear them: talk about it briefly and never write URLs or markdown images for them.

Now: {now}
User's name: {name}
{skills}
What you know about them (true facts from earlier chats; use them when asked "what do you know about me"):
{facts}
{recall}"""

EXTRACT_PROMPT = """Extract durable facts about the USER from this exchange: name, age, city, job, studies, family, \
friends, pets, likes/dislikes, goals, upcoming events, feelings that matter, preferences for how to talk.
Rules: only facts stated or clearly implied by the user; third person, short, in English; skip small talk.
Return a JSON array of strings only, e.g. ["Name is Riya", "Has a maths exam on Friday"]. Return [] if nothing new.

Already known:
{known}

Exchange:
User: {user}
Friend: {bot}"""


@dataclass
class Reply:
    text: str
    attachments: list[dict] = field(default_factory=list)


class Agent:
    def __init__(self, db: Database, ai: WorkersAI):
        self.db = db
        self.ai = ai
        self._locks: dict[int, asyncio.Lock] = {}
        self._bg: set[asyncio.Task] = set()

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _safe_embed(self, text: str) -> list[float] | None:
        try:
            vecs = await self.ai.embed([text])
            return vecs[0] if vecs else None
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding failed: %s", exc)
            return None

    async def build_system_prompt(self, user_id: int, query_emb: list[float] | None,
                                  oldest_recent_id: int) -> str:
        user = await self.db.get_user(user_id) or {}
        memories = await self.db.search_memories(user_id, query_emb, limit=10)
        facts = "\n".join(f"- {m['content']}" for m in memories) or "- (nothing yet; get to know them)"
        recall = ""
        if query_emb:
            similar = await self.db.similar_messages(user_id, query_emb, oldest_recent_id, limit=4)
            if similar:
                lines = [f"- [{m['created_at']:%d %b %Y}] {'They' if m['role'] == 'user' else 'You'}: "
                         f"{m['content'][:240]}" for m in similar]
                recall = "Earlier moments that may be relevant:\n" + "\n".join(lines)
        user_skills = await self.db.list_skills(user_id, enabled_only=True)
        skills_line = ("Custom skills they built: " + ", ".join(s["name"] for s in user_skills)) if user_skills else ""
        return PERSONA.format(
            bot=config.BOT_NAME,
            now=datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC"),
            name=user.get("display_name") or "unknown (ask casually)",
            skills=skills_line, facts=facts, recall=recall,
        )

    async def respond(self, user_id: int, text: str, channel: str = "web") -> Reply:
        async with self._lock(user_id):
            return await self._respond(user_id, text, channel)

    async def _respond(self, user_id: int, text: str, channel: str) -> Reply:
        query_emb = await self._safe_embed(text)
        recent = await self.db.recent_messages(user_id, config.HISTORY_WINDOW)
        oldest = recent[0]["id"] if recent else 2 ** 62
        system = await self.build_system_prompt(user_id, query_emb, oldest)
        system += "\n\nLanguage for this reply: " + language_hint(text)
        await self.db.add_message(user_id, "user", text, channel, embedding=query_emb)

        messages = [{"role": "system", "content": system}, *history_to_messages(recent),
                    {"role": "user", "content": text}]
        if len(messages) > 1 and messages[-2]["role"] == "user":  # keep strict alternation
            messages[-2]["content"] += "\n" + messages.pop()["content"]

        tool_defs, skill_map = await tools.available_tools(self.db, user_id)
        ctx = tools.ToolContext(db=self.db, ai=self.ai, user_id=user_id)
        answer = ""
        try:
            for step in range(config.MAX_TOOL_STEPS + 1):
                use_tools = tool_defs if step < config.MAX_TOOL_STEPS else None
                result = await self.ai.chat(messages, use_tools)
                if not result.tool_calls:
                    answer = result.content
                    break
                messages.append({"role": "assistant", "content": result.content or "", "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in result.tool_calls]})
                for call in result.tool_calls[:3]:
                    log.info("user %s -> tool %s %s", user_id, call.name, str(call.arguments)[:200])
                    output = await tools.execute(call.name, call.arguments, ctx, skill_map)
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output})
                for call in result.tool_calls[3:]:
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                     "content": "Skipped: too many tool calls at once."})
                if any(c.name == "create_skill" for c in result.tool_calls):
                    tool_defs, skill_map = await tools.available_tools(self.db, user_id)
        except AIError as exc:
            log.error("chat failed: %s", exc)
            answer = ("Yaar, my brain (the AI service) is not responding right now. "
                      "Give me a minute and try again?")
        answer = clean_answer(answer, bool(ctx.attachments))
        if not answer.strip():
            answer = "Done! 🙂" if ctx.attachments else "Hmm, I lost my words for a second. Say that again?"

        ans_emb = await self._safe_embed(answer)
        await self.db.add_message(user_id, "assistant", answer, channel, ctx.attachments, ans_emb)
        self._spawn(self._extract_memories(user_id, text, answer))
        return Reply(text=answer, attachments=ctx.attachments)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _extract_memories(self, user_id: int, user_text: str, bot_text: str) -> None:
        if len(user_text) < 8:
            return
        try:
            known = await self.db.list_memories(user_id)
            prompt = EXTRACT_PROMPT.format(
                known="\n".join(f"- {m['content']}" for m in known[:40]) or "(none)",
                user=user_text[:1500], bot=bot_text[:800])
            raw = await self.ai.complete("You extract memory facts as strict JSON.", prompt,
                                         max_tokens=250, temperature=0.0)
            facts = parse_fact_list(raw)
            if not facts:
                return
            embeddings = await self.ai.embed(facts)
            for fact, emb in zip(facts, embeddings):
                await self.db.upsert_memory(user_id, fact, emb)
                if fact.lower().startswith(("name is ", "user's name is ", "their name is ")):
                    user = await self.db.get_user(user_id)
                    if user and not user.get("display_name"):
                        await self.db.set_display_name(user_id, fact.split(" is ", 1)[1].strip(" ."))
        except Exception as exc:  # noqa: BLE001
            log.warning("memory extraction failed: %s", exc)

    async def wait_background(self) -> None:
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
