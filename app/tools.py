"""Built-in tools plus dynamically loaded user skills."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
import typing

import httpx
from bs4 import BeautifulSoup

from . import config, media, sandbox, skills
from .ai import WorkersAI
from .db import Database

log = logging.getLogger("dost.tools")


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


BUILTIN_TOOLS = [
    _fn("web_search", "Search the web for current facts, news, weather.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("fetch_webpage", "Read text of a web page.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("search_images", "Find photos on the internet.",
        {"query": {"type": "string"}, "count": {"type": "integer"}}, ["query"]),
    _fn("generate_image", "Generate an AI drawing.",
        {"prompt": {"type": "string"}}, ["prompt"]),
    _fn("run_python", "Execute Python 3 code.",
        {"code": {"type": "string"}}, ["code"]),
    _fn("remember", "Save durable fact about user.",
        {"fact": {"type": "string"}}, ["fact"]),
    _fn("create_skill",
        "Create custom skill.",
        {"name": {"type": "string"}, "request": {"type": "string"}, "secrets": {"type": "object"}},
        ["name", "request"]),
    _fn("update_skill",
        "Update custom skill.",
        {"name": {"type": "string"}, "request": {"type": "string"}, "secrets": {"type": "object"}},
        ["name", "request"]),
    _fn("view_skill_code",
        "Inspect the full Python source code, instructions and parameters of a skill.",
        {"name": {"type": "string"}},
        ["name"]),
    _fn("list_skills", "List user skills.", {}, []),
    _fn("delete_skill", "Delete user skill.", {"name": {"type": "string"}}, ["name"]),
    _fn("list_public_skills", "List community skills.", {}, []),
    _fn("share_skill_publicly", "Share skill publicly.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("install_skill", "Install a community skill (e.g. 'muzo').",
        {"name": {"type": "string"}, "skill_id": {"type": "integer"}}, []),
    _fn("set_reminder",
        "Set a reminder for the user at a specified date/time or relative delay (e.g. at 5pm, tomorrow 10am, in 20 minutes).",
        {
            "reminder": {"type": "string", "description": "What to remind the user about (e.g. 'stand up and stretch', 'take medicine', 'call mom')"},
            "time": {"type": "string", "description": "When to remind: ISO 8601 string (e.g. '2026-11-04T17:00:00+05:30') OR relative description (e.g. 'in 30 minutes', 'at 5pm')"}
        },
        ["reminder", "time"]),
    _fn("list_reminders", "List user's active pending reminders.", {}, []),
]


@dataclass
class ToolContext:
    db: Database
    ai: WorkersAI
    user_id: int
    channel: str = "web"
    chat_id: str = ""
    attachments: list[dict] = field(default_factory=list)
    status_callback: typing.Callable[[str], typing.Awaitable[None]] | None = None


def _clip(text: str, n: int = 3500) -> str:
    return text if len(text) <= n else text[:n] + "…"


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS

    def _run() -> list[dict]:
        return DDGS().text(query, max_results=max_results) or []
    return await asyncio.to_thread(_run)


async def image_search(query: str, count: int = 2) -> list[dict]:
    from ddgs import DDGS

    def _run() -> list[dict]:
        return DDGS().images(query, max_results=max(count * 3, 6)) or []
    return await asyncio.to_thread(_run)


async def fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (DostBot)"}) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        return r.text[:6000]
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
    return re.sub(r"[ \t]+", " ", text).strip()


async def _image_ok(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.head(url)
        return r.status_code < 400 and r.headers.get("content-type", "image").startswith("image")
    except httpx.HTTPError:
        return False


async def available_tools(db: Database, user_id: int) -> tuple[list[dict], dict[str, dict]]:
    user_skills = await db.list_skills(user_id, enabled_only=True)
    skill_map = {skills.tool_name(s["name"]): s for s in user_skills}
    return BUILTIN_TOOLS + [skills.as_tool(s) for s in user_skills], skill_map


async def execute(name: str, args: dict, ctx: ToolContext, skill_map: dict[str, dict]) -> str:
    """Run a tool and return the text the model sees. Media goes into ctx.attachments."""
    try:
        if name in skill_map:
            return await _run_user_skill(skill_map[name], args, ctx)
        handler = _HANDLERS.get(name)
        if not handler:
            return f"Unknown tool {name}."
        return await handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
        log.exception("tool %s failed", name)
        return f"Tool {name} failed: {type(exc).__name__}: {exc}"


async def _t_web_search(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query") or args.get("input") or "").strip()
    results = await web_search(query, 6)
    if not results:
        return "No results."
    lines = [f"- {r.get('title')}\n  {r.get('href')}\n  {r.get('body')}" for r in results]
    return _clip(f"Results for '{query}':\n" + "\n".join(lines))


async def _t_fetch(args: dict, ctx: ToolContext) -> str:
    url = str(args.get("url") or args.get("input") or "")
    if not url.startswith(("http://", "https://")):
        return "Please give a full http(s) URL."
    return _clip(await fetch_text(url), 5000)


async def _t_search_images(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query") or args.get("input") or "")
    count = max(1, min(int(args.get("count") or 2), 4))
    results = await image_search(query, count)
    sent: list[str] = []
    async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as c:
        for r in results:
            url = r.get("image")
            if url and await _image_ok(c, url):
                ctx.attachments.append({"type": "image", "url": url, "caption": r.get("title", "")[:180]})
                sent.append(r.get("title", "image"))
            if len(sent) >= count:
                break
    return f"Sent {len(sent)} image(s) to the user: {sent}" if sent else "Could not find a loadable image."


async def _t_generate_image(args: dict, ctx: ToolContext) -> str:
    prompt = str(args.get("prompt") or args.get("input") or "")
    if ctx.status_callback:
        try:
            await ctx.status_callback("Working on your image... 🎨 Just a moment!")
        except Exception:
            pass
    try:
        data = await ctx.ai.generate_image(prompt)
        url = media.save_bytes(data, ".jpg")
        ctx.attachments.append({"type": "image", "url": url, "caption": prompt[:180]})
        return "Image generated and sent to the user."
    except Exception as exc:
        log.exception("Image generation failed")
        return f"Image generation failed: {exc}"


async def _t_run_python(args: dict, ctx: ToolContext) -> str:
    if not config.ALLOW_CODE_EXEC:
        return "Code execution is disabled on this server."
    code = str(args.get("code") or args.get("input") or "")
    out = await sandbox.run_python(code)
    ctx.attachments.extend(out.attachments)
    status = "ok" if out.ok else "error"
    return _clip(f"status: {status}\nstdout:\n{out.stdout}\nstderr:\n{out.error}", 3000)


async def _t_remember(args: dict, ctx: ToolContext) -> str:
    fact = str(args.get("fact") or args.get("input") or "").strip()
    if not fact:
        return "Nothing to remember."
    emb = (await ctx.ai.embed([fact]))[0]
    await ctx.db.upsert_memory(ctx.user_id, fact, emb)
    return f"Saved to memory: {fact}"


async def _t_create_skill(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or "custom_skill"))
    request = str(args.get("request") or args.get("input") or "")
    secrets = args.get("secrets") or {}
    if isinstance(secrets, str):
        try:
            secrets = json.loads(secrets)
        except json.JSONDecodeError:
            secrets = {"API_KEY": secrets}
    secrets = {str(k).upper(): str(v) for k, v in secrets.items()}
    existing = await ctx.db.get_skill(ctx.user_id, name)
    existing_code = existing["code"] if existing else ""
    spec = await skills.generate_skill(ctx.ai, name=name, request=request, secrets=secrets,
                                       existing_code=existing_code, model=config.CODE_MODEL)
    await ctx.db.save_skill(ctx.user_id, name, spec["description"], request,
                            spec["parameters"], spec["code"], secrets)
    tested = "and passed a live self-test" if spec["tested"] else (spec["test_output"] or "(not self-tested)")
    action = "updated" if existing else "built"
    return (f"Skill '{name}' was {action} {tested}. It is available as tool {skills.tool_name(name)}. "
            f"Description: {spec['description']}. The user can review or edit its code in the web app.")


async def _t_update_skill(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or ""))
    if not name:
        return "Please specify the skill name to update."
    existing = await ctx.db.get_skill(ctx.user_id, name)
    if not existing:
        return f"No existing skill named '{name}' found. Use create_skill to create it first."
    request = str(args.get("request") or args.get("input") or "")
    new_secrets = args.get("secrets") or {}
    if isinstance(new_secrets, str):
        try:
            new_secrets = json.loads(new_secrets)
        except json.JSONDecodeError:
            new_secrets = {"API_KEY": new_secrets}
    secrets = dict(existing.get("secrets") or {})
    secrets.update({str(k).upper(): str(v) for k, v in new_secrets.items()})

    full_request = f"Update and improve skill '{name}'. Previous instructions:\n{existing.get('instructions')}\n\nUpdate requested:\n{request}"
    spec = await skills.generate_skill(ctx.ai, name=name, request=full_request, secrets=secrets,
                                       existing_code=existing.get("code", ""), model=config.CODE_MODEL)
    await ctx.db.save_skill(ctx.user_id, name, spec["description"], full_request,
                            spec["parameters"], spec["code"], secrets)
    tested = "and passed a live self-test" if spec["tested"] else (spec["test_output"] or "(not self-tested)")
    return (f"Skill '{name}' was updated {tested}. Description: {spec['description']}.")


async def _t_view_skill_code(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or args.get("input") or ""))
    if not name:
        return "Please specify the skill name to inspect."
    s = await ctx.db.get_skill(ctx.user_id, name)
    if not s:
        # Check if it's a public skill
        public_skills = await ctx.db.list_public_skills(ctx.user_id)
        s = next((item for item in public_skills if item["name"].lower() == name.lower()), None)
    if not s:
        return f"Skill '{name}' not found."

    params_str = json.dumps(s.get("parameters") or {}, indent=2)
    return (
        f"Skill: {s['name']}\n"
        f"Description: {s.get('description', '')}\n"
        f"Parameters: {params_str}\n\n"
        f"Source Code:\n```python\n{s.get('code', '')}\n```"
    )


async def _t_list_skills(args: dict, ctx: ToolContext) -> str:
    items = await ctx.db.list_skills(ctx.user_id)
    if not items:
        return "The user has no custom skills yet."
    return "\n".join(f"- {s['name']} ({'on' if s['enabled'] else 'off'}): {s['description']}" for s in items)


async def _t_delete_skill(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or args.get("input") or ""))
    ok = await ctx.db.delete_skill(ctx.user_id, name)
    return f"Deleted skill {name}." if ok else f"No skill named {name}."


async def _t_list_public_skills(args: dict, ctx: ToolContext) -> str:
    items = await ctx.db.list_public_skills(ctx.user_id)
    if not items:
        return "No public skills found in the community catalog yet."
    return "Public Community Skills available:\n" + "\n".join(
        f"- ID: {s['id']} | '{s['name']}' by {s.get('author_name') or 'Anonymous'}: {s['description']}"
        for s in items
    )


async def _t_share_skill_publicly(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or args.get("input") or ""))
    ok = await ctx.db.update_skill_fields(ctx.user_id, name, is_public=True)
    if ok:
        return f"Skill '{name}' is now public and available for other users to install!"
    return f"Could not find skill '{name}' to make public."


async def _t_install_skill(args: dict, ctx: ToolContext) -> str:
    skill_id = args.get("skill_id")
    name = str(args.get("name") or "").strip()
    if not skill_id and name:
        public_skills = await ctx.db.list_public_skills(ctx.user_id)
        match = next((s for s in public_skills if s["name"].lower() == name.lower()), None)
        if match:
            skill_id = match["id"]
    if not skill_id:
        return "Please specify the skill_id or name of the public skill to install."
    installed = await ctx.db.install_public_skill(ctx.user_id, int(skill_id))
    if installed:
        return f"Successfully installed public skill '{installed['name']}'! You can now use it in chat."
    return f"Failed to find or install public skill with ID {skill_id}."


async def _run_user_skill(skill: dict, args: dict, ctx: ToolContext) -> str:
    # If the skill is muzo, guarantee that lossless is only enabled when explicitly asked
    if skill["name"] == "muzo":
        query = str(args.get("query") or "")
        explicit_flac = bool(
            args.get("lossless")
            or "flac" in query.lower()
            or "lossless" in query.lower()
        )
        args["lossless"] = explicit_flac

    out = await sandbox.run_skill(skill["code"], args, skill.get("secrets") or {}, user_id=ctx.user_id)
    if not out.ok:
        return f"Skill {skill['name']} failed: {out.error}"
    ctx.attachments.extend(out.attachments)
    text = out.result.get("text") or json.dumps(
        {k: v for k, v in out.result.items() if k not in ("images", "audio", "video", "files")}, default=str)
    extra = (f"\n[{len(out.attachments)} media item(s) were ALREADY delivered to the user. "
             "Do not write any URLs or image markdown.]") if out.attachments else ""
    return _clip(f"{text}{extra}")


async def _parse_reminder_datetime(time_str: str) -> datetime | None:
    """Parse time string into timezone-aware datetime (defaulting to Asia/Kolkata / IST if not specified)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    tz_ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    raw = time_str.strip().lower()

    # 1. Try ISO format
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_ist)
        return dt
    except Exception:
        pass

    # 2. Relative delays: "in X minutes/hours/seconds"
    m_rel = re.search(r'(?:in\s+)?(\d+(?:\.\d+)?)\s*(second|sec|s|minute|min|m|hour|hr|h|day|d)s?', raw)
    if m_rel:
        val = float(m_rel.group(1))
        unit = m_rel.group(2)
        if unit.startswith('s'):
            delta = timedelta(seconds=val)
        elif unit.startswith('m'):
            delta = timedelta(minutes=val)
        elif unit.startswith('h'):
            delta = timedelta(hours=val)
        elif unit.startswith('d'):
            delta = timedelta(days=val)
        else:
            delta = timedelta(minutes=val)
        return now_ist + delta

    # 3. Time of day: "at 5pm", "5:30 pm", "at 17:00", "tomorrow 9am"
    day_offset = 0
    if "tomorrow" in raw:
        day_offset = 1

    m_time = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', raw)
    if m_time:
        hour = int(m_time.group(1))
        minute = int(m_time.group(2)) if m_time.group(2) else 0
        ampm = m_time.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)
        # If no day was specified and time has already passed today, assume tomorrow
        if day_offset == 0 and target <= now_ist:
            target += timedelta(days=1)
        return target

    return None


async def _t_set_reminder(args: dict, ctx: ToolContext) -> str:
    reminder_text = str(args.get("reminder") or args.get("text") or "").strip()
    time_str = str(args.get("time") or args.get("when") or "").strip()
    if not reminder_text:
        return "Please describe what to remind the user about."
    if not time_str:
        return "Please specify when to send the reminder."

    target_dt = await _parse_reminder_datetime(time_str)
    if not target_dt:
        return f"Could not parse reminder time '{time_str}'. Please provide ISO time or relative time like 'in 20 minutes', 'at 5pm'."

    from zoneinfo import ZoneInfo
    tz_ist = ZoneInfo("Asia/Kolkata")
    local_target = target_dt.astimezone(tz_ist)
    chat_id = ctx.chat_id
    if not chat_id and ctx.channel == "telegram":
        # Look up identity
        identity = await ctx.db.pool.fetchrow(
            "SELECT external_id FROM identities WHERE user_id=$1 AND channel='telegram'", ctx.user_id)
        if identity:
            chat_id = identity["external_id"]

    rid = await ctx.db.create_reminder(
        user_id=ctx.user_id,
        reminder_text=reminder_text,
        remind_at=target_dt,
        channel=ctx.channel,
        chat_id=chat_id,
    )
    formatted = local_target.strftime("%I:%M %p (%d %b %Y)")
    return f"Reminder #{rid} set successfully for {formatted} (IST) to remind: '{reminder_text}'. I will message you at that time!"


async def _t_list_reminders(args: dict, ctx: ToolContext) -> str:
    items = await ctx.db.list_user_reminders(ctx.user_id, status="pending")
    if not items:
        return "No pending reminders found."
    from zoneinfo import ZoneInfo
    tz_ist = ZoneInfo("Asia/Kolkata")
    lines = []
    for r in items:
        dt = r["remind_at"].astimezone(tz_ist).strftime("%I:%M %p on %d %b %Y")
        lines.append(f"- #{r['id']} at {dt}: {r['reminder_text']}")
    return "Pending reminders:\n" + "\n".join(lines)


_HANDLERS = {
    "web_search": _t_web_search,
    "fetch_webpage": _t_fetch,
    "search_images": _t_search_images,
    "generate_image": _t_generate_image,
    "run_python": _t_run_python,
    "remember": _t_remember,
    "create_skill": _t_create_skill,
    "update_skill": _t_update_skill,
    "view_skill_code": _t_view_skill_code,
    "list_skills": _t_list_skills,
    "delete_skill": _t_delete_skill,
    "list_public_skills": _t_list_public_skills,
    "share_skill_publicly": _t_share_skill_publicly,
    "install_skill": _t_install_skill,
    "set_reminder": _t_set_reminder,
    "list_reminders": _t_list_reminders,
}
