# Dost: a best-friend bot for Telegram and the web

Dost chats like a close friend in Hindi, Hinglish, English, Spanish, Tamil, Arabic and other languages. It always replies in the language and script you wrote in. It remembers you, searches the internet, sends photos, draws images, writes and runs code, and **builds new skills for itself** from your instructions and API keys.

Every AI call goes to **Cloudflare Workers AI**, and by default only free models are used (models with no per-token price; they draw from the daily free allocation):

| Job | Model |
|---|---|
| Chat + tool calling | `@cf/ibm-granite/granite-4.0-h-micro` (fallback `@cf/daily-co/phonellm-mini-alpha-2`) |
| Writing skill code | `@cf/ibm-granite/granite-4.0-h-micro` |
| Memory embeddings (multilingual, 768-d) | `@cf/google/embeddinggemma-300m` |
| Image generation | `@cf/black-forest-labs/flux-1-schnell` |
| Voice notes → text | `@cf/openai/whisper` |
| Understanding photos you send | `@cf/llava-hf/llava-1.5-7b-hf` |

The account ID and API token are hardcoded in `app/config.py`, as requested. Environment variables still override them.

## Run it

```bash
cp .env.example .env        # put your TELEGRAM_BOT_TOKEN from @BotFather here, change WEB_PASSWORD + SECRET_KEY
docker compose up -d --build
```

- Web chat: http://localhost:8080 (sign in with any name + `WEB_PASSWORD`)
- Telegram: message your bot. `/start`, `/memory`, `/skills`, `/reset`, `/forget`, `/link`
- Health: http://localhost:8080/health

Needs Docker Compose v2.24 or newer, because the optional `env_file` syntax depends on it. Data lives in two volumes: `pgdata` (Postgres) and `botdata` (generated media).

## How it works

```
Telegram ─┐                     ┌─ Workers AI (chat, embeddings, FLUX, Whisper, LLaVA)
          ├─ FastAPI + Agent ───┼─ Postgres 16 + pgvector (users, messages, memories, skills)
Web UI  ──┘    (one process)    └─ Sandboxed subprocesses (user skills, run_python)
```

**Context and memory**
- Every message is stored with a vector embedding.
- Each reply sees the last 16 messages, plus the older messages most similar to what you just said (vector search), plus long-term facts about you.
- After each exchange a background step pulls out durable facts ("Name is Riya", "Exam on Friday"). Near-duplicates (cosine ≥ 0.9) are merged.
- A per-turn language hint keeps small models in your script (Hinglish stays in Latin letters, Hindi stays in Devanagari).
- Telegram and web share one profile after `/link`.

**Tools:** `web_search`, `fetch_webpage`, `search_images` (real photos from the internet), `generate_image`, `run_python`, `remember`, `create_skill`, `list_skills`, `delete_skill`, plus every skill you build (`skill_<name>`).

**Self-built skills.** Say something like:

> make a skill called song_player. Search with `GET https://my-music-api/search?q=...&key=KEY` (returns `items[].id`), stream with `GET https://my-music-api/stream/{id}?key=KEY` (returns mp3). My key is abc123.

What happens next:
1. The bot calls `create_skill` and the code model writes `run(args, secrets, ctx)`.
2. Your keys go into the skill's `secrets`; they are not written into the code.
3. The bot runs a self-test and, if it fails, retries with the error, up to 3 attempts.
4. The skill is saved to Postgres and becomes a tool right away.

A skill returns `{"text", "images", "audio", "video", "files"}`. URLs or downloaded files are sent as real photos and audio on Telegram, and as players in the web UI. You can view, edit, turn off or delete skills in the web side panel, or build one there with a form.

**Sandbox:** skill and `run_python` code runs in a separate process with a timeout, a 1 GB memory cap and no access to the bot's environment variables (the Cloudflare token is not visible to it). It still has network access and runs inside the container, so only give access to people you trust. Use `TELEGRAM_ALLOWED_USERS`, a strong `WEB_PASSWORD`, or set `ALLOW_CODE_EXEC=false`.

## Develop

```bash
pip install -r requirements.txt pytest
python -m pytest                 # unit tests; the integration test also runs if Postgres+pgvector is reachable
python -m scripts.live_smoke     # real conversation against Workers AI (uses free neurons)
```

## Files

- `app/config.py`: settings and hardcoded Cloudflare credentials
- `app/ai.py`: Workers AI REST client (chat, tools, embeddings, images, speech, vision)
- `app/db.py`: schema and queries (pgvector)
- `app/agent.py`: persona, context building, tool loop, memory extraction
- `app/tools.py`: built-in tools and the skill dispatcher
- `app/skills.py`: skill generation and self-testing
- `app/sandbox.py`, `app/skill_runner.py`: isolated code execution
- `app/telegram_bot.py`: Telegram front-end (text, voice notes, photos, commands)
- `app/main.py`: web API, static UI, lifecycle
- `web/`: chat UI (see `DESIGN.md`)

## Skills lifecycle (chat or UI)
- **Create**: "make a skill called weather using wttr.in" (`create_skill`), self-tested automatically.
- **Upgrade / fix**: "upgrade skill weather so it also shows tomorrow" (`update_skill`); re-check with `test_skill`.
- **Private by default**. **Make public**: "share skill weather" or the *Make public* button (web) / `/skills` buttons (Telegram). **Make private** reverses it. Secrets are never shared.
- **Community**: browse and install other people's skills (`/community`, sidebar); add your own keys afterwards.
- **Switch off / delete**: `set_skill_enabled`, `delete_skill`, or the panel buttons.

## Internet tools
`web_search` (text/news), `fetch_webpage` (readable text, raw HTML source, link list, paging via offset) and `http_request` (any public API). Private/internal addresses are blocked.
