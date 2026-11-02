"""FastAPI app: web chat API, static UI and the Telegram bot lifecycle."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, media, skills
from .agent import Agent
from .ai import WorkersAI
from .db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dost")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class State:
    db: Database
    ai: WorkersAI
    agent: Agent
    telegram = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    media.ensure_dirs()
    state.db = Database()
    await state.db.connect()
    state.ai = WorkersAI()
    state.agent = Agent(state.db, state.ai)
    if config.TELEGRAM_BOT_TOKEN:
        from .telegram_bot import TelegramBot
        try:
            state.telegram = TelegramBot(state.agent)
            await state.telegram.start()
        except Exception as exc:  # noqa: BLE001 - web keeps working without Telegram
            log.error("Telegram bot failed to start: %s", exc)
            state.telegram = None
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set: running the web chat only")
    yield
    if state.telegram:
        await state.telegram.stop()
    await state.agent.wait_background()
    await state.ai.close()
    await state.db.close()


app = FastAPI(title=f"{config.BOT_NAME} bot", lifespan=lifespan)


# --- auth ------------------------------------------------------------------
def _sign(user_id: int) -> str:
    sig = hmac.new(config.SECRET_KEY.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{sig}"


def _verify(token: str) -> int | None:
    try:
        uid_s, _ = token.split(".", 1)
        uid = int(uid_s)
    except ValueError:
        return None
    return uid if hmac.compare_digest(_sign(uid), token) else None


async def current_user(authorization: str = Header(default="")) -> int:
    uid = _verify(authorization.removeprefix("Bearer ").strip())
    if uid is None:
        raise HTTPException(401, "Please sign in again.")
    return uid


class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    password: str


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class SkillIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    request: str = Field(min_length=10, max_length=12000)
    secrets: dict[str, str] = {}


class SkillPatch(BaseModel):
    code: str | None = None
    enabled: bool | None = None
    secrets: dict[str, str] | None = None
    description: str | None = None


class LinkIn(BaseModel):
    code: str


def _web_identity(name: str) -> str:
    return name.strip().lower()


def _jsonable(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
        out.append(r)
    return out


# --- routes ----------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "telegram": bool(state.telegram), "models": config.CHAT_MODELS}


@app.post("/api/login")
async def login(body: LoginIn):
    if not hmac.compare_digest(body.password, config.WEB_PASSWORD):
        raise HTTPException(401, "Wrong password.")
    uid = await state.db.get_or_create_user("web", _web_identity(body.name), body.name.strip())
    user = await state.db.get_user(uid)
    return {"token": _sign(uid), "name": user["display_name"] or body.name, "bot": config.BOT_NAME}


@app.get("/api/me")
async def me(uid: int = Depends(current_user)):
    user = await state.db.get_user(uid)
    if not user:
        raise HTTPException(401, "Unknown user.")
    return {"id": uid, "name": user["display_name"], "bot": config.BOT_NAME, "telegram": bool(state.telegram)}


@app.get("/api/history")
async def history(uid: int = Depends(current_user)):
    return _jsonable(await state.db.history(uid, 80))


@app.post("/api/chat")
async def chat(body: ChatIn, uid: int = Depends(current_user)):
    reply = await state.agent.respond(uid, body.text.strip(), channel="web")
    return {"text": reply.text, "attachments": reply.attachments}


@app.post("/api/voice")
async def voice(file: UploadFile = File(...), uid: int = Depends(current_user)):
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Recording too long.")
    text = await state.ai.transcribe(data)
    if not text:
        raise HTTPException(422, "Couldn't hear anything in that recording.")
    reply = await state.agent.respond(uid, f"(voice note) {text}", channel="web")
    return {"transcript": text, "text": reply.text, "attachments": reply.attachments}


@app.post("/api/reset")
async def reset(uid: int = Depends(current_user)):
    await state.db.archive_history(uid)
    return {"ok": True}


@app.get("/api/memories")
async def memories(uid: int = Depends(current_user)):
    return _jsonable(await state.db.list_memories(uid))


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: int, uid: int = Depends(current_user)):
    return {"ok": await state.db.delete_memory(uid, memory_id)}


def _skill_out(s: dict) -> dict:
    s = _jsonable([s])[0]
    s["secrets"] = sorted((s.get("secrets") or {}).keys())  # never send values back
    return s


@app.get("/api/skills")
async def list_skills(uid: int = Depends(current_user)):
    return [_skill_out(s) for s in await state.db.list_skills(uid)]


@app.post("/api/skills")
async def create_skill(body: SkillIn, uid: int = Depends(current_user)):
    name = skills.slugify(body.name)
    secrets = {k.strip().upper(): v for k, v in body.secrets.items() if k.strip()}
    try:
        spec = await skills.generate_skill(state.ai, name=name, request=body.request, secrets=secrets,
                                           model=config.CODE_MODEL)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await state.db.save_skill(uid, name, spec["description"], body.request, spec["parameters"], spec["code"], secrets)
    return {**_skill_out(await state.db.get_skill(uid, name)), "tested": spec["tested"],
            "test_output": spec["test_output"]}


@app.patch("/api/skills/{name}")
async def patch_skill(name: str, body: SkillPatch, uid: int = Depends(current_user)):
    skill = await state.db.get_skill(uid, name)
    if not skill:
        raise HTTPException(404, "No such skill.")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "code" in fields:
        err = skills.validate_code(fields["code"])
        if err:
            raise HTTPException(422, err)
    if "secrets" in fields:
        fields["secrets"] = {**(skill.get("secrets") or {}),
                             **{k.strip().upper(): v for k, v in fields["secrets"].items() if k.strip()}}
    await state.db.update_skill_fields(uid, name, **fields)
    return _skill_out(await state.db.get_skill(uid, name))


@app.delete("/api/skills/{name}")
async def delete_skill(name: str, uid: int = Depends(current_user)):
    return {"ok": await state.db.delete_skill(uid, name)}


@app.post("/api/link/code")
async def link_code(uid: int = Depends(current_user)):
    code = pysecrets.token_hex(3).upper()
    await state.db.create_link_code(uid, code)
    return {"code": code, "hint": f"Send /link {code} to the bot on Telegram within 15 minutes."}


@app.post("/api/link")
async def link_redeem(body: LinkIn, uid: int = Depends(current_user)):
    """Redeem a code from Telegram's /link: this web login joins the Telegram user."""
    row = await state.db.pool.fetchrow(
        "SELECT external_id FROM identities WHERE channel='web' AND user_id=$1 LIMIT 1", uid)
    if not row:
        raise HTTPException(400, "No web identity.")
    target = await state.db.redeem_link_code(body.code, "web", row["external_id"])
    if not target:
        raise HTTPException(400, "That code is invalid or expired.")
    user = await state.db.get_user(target)
    return {"token": _sign(target), "name": user["display_name"]}


app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR), check_dir=False), name="media")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")
