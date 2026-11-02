"""Live smoke test against Cloudflare Workers AI and a local Postgres.

Usage: python -m scripts.live_smoke
"""
import asyncio
import os
import time

from app.agent import Agent
from app.ai import WorkersAI
from app.db import Database

PROMPTS = [
    "Hey yaar, main Kabir hoon. Aaj office mein bahut thak gaya, Delhi se hoon aur cricket ka pagal hoon",
    "मुझे पता है तुम search कर सकते हो, आज का दिल्ली का मौसम कैसा है?",
    "send me 2 pictures of Kedarnath temple",
    "Make a skill named song_player: search songs with the free iTunes Search API "
    "GET https://itunes.apple.com/search?term=<query>&media=music&entity=song&limit=1 . The JSON has results[0] "
    "with trackName, artistName, previewUrl (an m4a audio clip) and artworkUrl100. Return the audio previewUrl "
    "with title 'trackName - artistName', and the artwork as an image.",
    "play Kesariya by Arijit on song_player",
    "mera naam kya hai aur mujhe kya pasand hai?",
]


async def main() -> None:
    db = Database(os.getenv("DATABASE_URL", "postgresql://dost:dost@localhost:5432/dost"))
    await db.connect()
    ai = WorkersAI()
    agent = Agent(db, ai)
    uid = await db.get_or_create_user("smoke", str(int(time.time())), "")
    for p in PROMPTS:
        t = time.time()
        r = await agent.respond(uid, p, "smoke")
        print(f"\n>>> {p}\n<<< ({time.time() - t:.1f}s) {r.text[:600]}")
        for a in r.attachments:
            print("    [att]", a["type"], a["url"][:120], a.get("title", ""))
        await agent.wait_background()
    print("\nMEMORIES:", [m["content"] for m in await db.list_memories(uid)])
    print("SKILLS:", [(s["name"], s["description"]) for s in await db.list_skills(uid)])
    await ai.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
