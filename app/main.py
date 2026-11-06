"""FastAPI app: web chat API, static UI and the Telegram bot lifecycle."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, media, skills
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
    username: str | None = None
    name: str | None = None
    password: str


class SignupIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=3, max_length=100)
    name: str = Field(min_length=1, max_length=50)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class SkillIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    request: str = Field(min_length=10, max_length=12000)
    secrets: dict[str, str] = {}
    is_public: bool = False


class SkillPatch(BaseModel):
    code: str | None = None
    enabled: bool | None = None
    secrets: dict[str, str] | None = None
    description: str | None = None
    is_public: bool | None = None


class LinkIn(BaseModel):
    code: str


def _hash_password(pw: str) -> str:
    return auth.hash_password(pw)


def _verify_password(pw: str, hashed: str) -> bool:
    return auth.verify_password(pw, hashed)


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


@app.post("/api/signup")
async def signup(body: SignupIn):
    username = body.username.strip().lower()
    existing = await state.db.get_user_by_username(username)
    if existing:
        raise HTTPException(400, "Username already taken.")
    pw_hash = _hash_password(body.password)
    uid = await state.db.create_user_account(username, pw_hash, body.name.strip())
    user = await state.db.get_user(uid)
    return {"token": _sign(uid), "name": user["display_name"] or username, "bot": config.BOT_NAME}


@app.post("/api/login")
async def login(body: LoginIn):
    user_identifier = (body.username or body.name or "").strip()
    if not user_identifier:
        raise HTTPException(400, "Username or name required.")
    
    # Check if registered user exists with password hash
    user = await state.db.get_user_by_username(user_identifier)
    if user and user.get("password_hash"):
        if not _verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Invalid username or password.")
        return {"token": _sign(user["id"]), "name": user["display_name"] or user_identifier, "bot": config.BOT_NAME}
    
    # Fallback to global web password for legacy/admin access
    if not hmac.compare_digest(body.password, config.WEB_PASSWORD):
        raise HTTPException(401, "Wrong password or user not found.")
    uid = await state.db.get_or_create_user("web", _web_identity(user_identifier), user_identifier)
    db_user = await state.db.get_user(uid)
    return {"token": _sign(uid), "name": db_user["display_name"] or user_identifier, "bot": config.BOT_NAME}


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


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, uid: int = Depends(current_user)):
    async def event_generator():
        try:
            async for ev in state.agent.respond_stream(uid, body.text.strip(), channel="web"):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            log.error("SSE stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


@app.post("/api/upload_image")
async def upload_image(file: UploadFile = File(...), caption: str = "", uid: int = Depends(current_user)):
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "Image file too large (max 15MB).")
    try:
        desc = await state.ai.describe_image(data)
    except Exception as exc:
        log.warning("Image vision analysis failed: %s", exc)
        desc = "an uploaded image"
    prompt_text = f"[I shared an image: {desc}] {caption}".strip()
    reply = await state.agent.respond(uid, prompt_text, channel="web")
    return {"description": desc, "text": reply.text, "attachments": reply.attachments}


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


@app.get("/api/skills/public")
async def list_public_skills(uid: int = Depends(current_user)):
    return [_skill_out(s) for s in await state.db.list_public_skills(uid)]


@app.post("/api/skills/install/{skill_id}")
async def install_skill(skill_id: int, uid: int = Depends(current_user)):
    installed = await state.db.install_public_skill(uid, skill_id)
    if not installed:
        raise HTTPException(404, "Public skill not found.")
    return _skill_out(installed)


@app.post("/api/skills")
async def create_skill(body: SkillIn, uid: int = Depends(current_user)):
    name = skills.slugify(body.name)
    secrets = {k.strip().upper(): v for k, v in body.secrets.items() if k.strip()}
    existing = await state.db.get_skill(uid, name)
    existing_code = existing["code"] if existing else ""
    try:
        spec = await skills.generate_skill(state.ai, name=name, request=body.request, secrets=secrets,
                                           existing_code=existing_code, model=config.CODE_MODEL)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await state.db.save_skill(uid, name, spec["description"], body.request, spec["parameters"],
                            spec["code"], secrets, is_public=body.is_public)
    return {**_skill_out(await state.db.get_skill(uid, name)), "tested": spec["tested"],
            "test_output": spec["test_output"]}


@app.patch("/api/skills/{name}")
async def patch_skill(name: str, body: SkillPatch, uid: int = Depends(current_user)):
    skill = await state.db.get_skill(uid, name)
    if not skill:
        raise HTTPException(404, "No such skill.")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "code" in fields:
        raise HTTPException(403, "Editing skill code directly is disabled for security. Skills can only be viewed or regenerated.")
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
