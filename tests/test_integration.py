"""End-to-end agent loop against a real Postgres+pgvector with a scripted fake AI.

Skipped automatically when the app dependencies or a database are not available.
"""
import asyncio
import hashlib
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import asyncpg  # noqa: F401
    import httpx  # noqa: F401
    import bs4  # noqa: F401
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise unittest.SkipTest(f"app dependencies not installed: {exc}")

from app import config
from app.agent import Agent
from app.db import Database
from app.parsing import ChatResult, ToolCall

DSN = os.getenv("TEST_DATABASE_URL", config.DATABASE_URL)


def _fake_vec(text: str) -> list[float]:
    vals = [0.0] * config.EMBED_DIM
    for word in text.lower().split():
        vals[int(hashlib.md5(word.encode()).hexdigest(), 16) % config.EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]


SKILL_ANSWER = '''```json
{"description": "Search and play songs from the user's music API", "parameters": {"type": "object",
 "properties": {"query": {"type": "string"}}, "required": ["query"]}, "test_args": {"query": "test"}}
```
```python
def run(args, secrets, ctx):
    base = secrets.get("STREAM_BASE")
    return {"text": "Now playing " + args["query"], "audio": [{"url": base + "/" + args["query"] + ".mp3", "title": args["query"]}]}
```'''


class FakeAI:
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        return [_fake_vec(t) for t in texts]

    async def complete(self, system, user, **kw):
        if "skill" in system.lower():
            return SKILL_ANSWER
        return '["Name is Aarav", "Loves Arijit Singh songs"]'

    async def chat(self, messages, tools=None, **kw):
        self.calls.append(messages)
        last = messages[-1]
        names = {t["function"]["name"] for t in tools or []}
        if last["role"] == "tool":
            return ChatResult(content=f"Ho gaya! ({last['content'][:60]})")
        text = last["content"].lower()
        if "make a song skill" in text:
            return ChatResult("", [ToolCall("c1", "create_skill", {
                "name": "Song Player", "request": "search and stream songs",
                "secrets": {"stream_base": "https://music.example"}})])
        if "play" in text and "skill_song_player" in names:
            return ChatResult("", [ToolCall("c2", "skill_song_player", {"query": "kesariya"})])
        return ChatResult(content="Arre yaar, bata kya hua?")


class AgentIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.db = Database(DSN)
        try:
            self.loop.run_until_complete(asyncio.wait_for(self.db.connect(), 5))
        except Exception as exc:  # noqa: BLE001
            self.loop.close()
            self.skipTest(f"no database: {exc}")
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [mock.patch.object(config, "DATA_DIR", tmp),
                        mock.patch.object(config, "MEDIA_DIR", tmp / "media")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        self.loop.run_until_complete(self.db.close())
        self.loop.close()

    def test_full_conversation_memory_and_skill(self):
        d = self.db

        async def scenario():
            ai = FakeAI()
            agent = Agent(d, ai)
            uid = await d.get_or_create_user("test", os.urandom(6).hex(), "")

            r1 = await agent.respond(uid, "Main Aarav hoon, mujhe Arijit Singh ke gaane pasand hai", "test")
            self.assertEqual(r1.text, "Arre yaar, bata kya hua?")
            await agent.wait_background()
            facts = [m["content"] for m in await d.list_memories(uid)]
            self.assertIn("Loves Arijit Singh songs", facts)
            self.assertEqual((await d.get_user(uid))["display_name"], "Aarav")

            # Memories and the language hint flow into the next system prompt.
            await agent.respond(uid, "kya yaad hai mere baare mein?", "test")
            system = ai.calls[-1][0]["content"]
            self.assertIn("Loves Arijit Singh songs", system)
            self.assertIn("Aarav", system)
            self.assertIn("Hinglish", system)

            # Self-built skill: created, self-tested, then used with media.
            r3 = await agent.respond(uid, "make a song skill with my api", "test")
            self.assertIn("Ho gaya", r3.text)
            skill = await d.get_skill(uid, "song_player")
            self.assertEqual(skill["secrets"], {"STREAM_BASE": "https://music.example"})

            r4 = await agent.respond(uid, "play kesariya", "test")
            self.assertEqual(r4.attachments, [{"type": "audio", "url": "https://music.example/kesariya.mp3",
                                               "caption": "", "title": "kesariya"}])
            hist = await d.history(uid)
            self.assertEqual(len(hist), 8)
            self.assertEqual(hist[-1]["attachments"][0]["type"], "audio")

            # Linking a second channel to the same user.
            await d.create_link_code(uid, "ABC123")
            self.assertEqual(await d.redeem_link_code("abc123", "web", "aarav-web"), uid)
            self.assertEqual(await d.get_or_create_user("web", "aarav-web"), uid)

        self.loop.run_until_complete(scenario())


if __name__ == "__main__":
    unittest.main()
