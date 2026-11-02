"""Built-in tools plus dynamically loaded user skills."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

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
    _fn("web_search", "Search the internet for current information, news, facts, prices, weather, lyrics, etc.",
        {"query": {"type": "string", "description": "search query"}}, ["query"]),
    _fn("fetch_webpage", "Read the text of a web page (use after web_search to get details).",
        {"url": {"type": "string"}}, ["url"]),
    _fn("search_images", "Find real photos/images on the internet and send them to the user.",
        {"query": {"type": "string"}, "count": {"type": "integer", "description": "1-4, default 2"}}, ["query"]),
    _fn("generate_image", "Create a new AI image/art/drawing from a description and send it to the user.",
        {"prompt": {"type": "string", "description": "detailed English description of the image"}}, ["prompt"]),
    _fn("run_python", "Execute Python 3 code to calculate, test or demonstrate code. Print the output.",
        {"code": {"type": "string"}}, ["code"]),
    _fn("remember", "Save an important long-term fact about the user (name, likes, plans, people in their life).",
        {"fact": {"type": "string", "description": "one short fact, third person, e.g. 'Loves Arijit Singh songs'"}},
        ["fact"]),
    _fn("create_skill",
        "Build a NEW permanent skill/tool when the user asks you to make a skill or integrate an API "
        "(e.g. a song player using their search + stream API). Put every API key, base URL or token the user "
        "gave into `secrets`.",
        {"name": {"type": "string", "description": "short snake_case name, e.g. song_player"},
         "request": {"type": "string", "description": "full description of what the skill must do, including "
                     "API endpoints, request/response formats and docs the user provided"},
         "secrets": {"type": "object", "description": "API keys/URLs as KEY_NAME: value"}},
        ["name", "request"]),
    _fn("list_skills", "List the custom skills this user has built.", {}, []),
    _fn("delete_skill", "Delete one of the user's custom skills.", {"name": {"type": "string"}}, ["name"]),
]


@dataclass
class ToolContext:
    db: Database
    ai: WorkersAI
    user_id: int
    attachments: list[dict] = field(default_factory=list)


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
    data = await ctx.ai.generate_image(prompt)
    url = media.save_bytes(data, ".jpg")
    ctx.attachments.append({"type": "image", "url": url, "caption": prompt[:180]})
    return "Image generated and sent to the user."


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
    spec = await skills.generate_skill(ctx.ai, name=name, request=request, secrets=secrets,
                                       model=config.CODE_MODEL)
    await ctx.db.save_skill(ctx.user_id, name, spec["description"], request,
                            spec["parameters"], spec["code"], secrets)
    tested = "and passed a live self-test" if spec["tested"] else (spec["test_output"] or "(not self-tested)")
    return (f"Skill '{name}' was built {tested}. It is now available as tool {skills.tool_name(name)}. "
            f"Description: {spec['description']}. The user can review or edit its code in the web app.")


async def _t_list_skills(args: dict, ctx: ToolContext) -> str:
    items = await ctx.db.list_skills(ctx.user_id)
    if not items:
        return "The user has no custom skills yet."
    return "\n".join(f"- {s['name']} ({'on' if s['enabled'] else 'off'}): {s['description']}" for s in items)


async def _t_delete_skill(args: dict, ctx: ToolContext) -> str:
    name = skills.slugify(str(args.get("name") or args.get("input") or ""))
    ok = await ctx.db.delete_skill(ctx.user_id, name)
    return f"Deleted skill {name}." if ok else f"No skill named {name}."


async def _run_user_skill(skill: dict, args: dict, ctx: ToolContext) -> str:
    out = await sandbox.run_skill(skill["code"], args, skill.get("secrets") or {}, user_id=ctx.user_id)
    if not out.ok:
        return f"Skill {skill['name']} failed: {out.error}"
    ctx.attachments.extend(out.attachments)
    text = out.result.get("text") or json.dumps(
        {k: v for k, v in out.result.items() if k not in ("images", "audio", "video", "files")}, default=str)
    extra = (f"\n[{len(out.attachments)} media item(s) were ALREADY delivered to the user. "
             "Do not write any URLs or image markdown.]") if out.attachments else ""
    return _clip(f"{text}{extra}")


_HANDLERS = {
    "web_search": _t_web_search,
    "fetch_webpage": _t_fetch,
    "search_images": _t_search_images,
    "generate_image": _t_generate_image,
    "run_python": _t_run_python,
    "remember": _t_remember,
    "create_skill": _t_create_skill,
    "list_skills": _t_list_skills,
    "delete_skill": _t_delete_skill,
}
