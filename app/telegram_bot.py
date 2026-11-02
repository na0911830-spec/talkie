"""Telegram front-end (long polling)."""
from __future__ import annotations

import io
import logging
import secrets as pysecrets

import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import config, media
from .agent import Agent, Reply
from .textfmt import md_to_telegram_html, split_message

log = logging.getLogger("dost.telegram")

HELP = (
    "Hey! I'm {bot}, your friend. Just talk to me in any language.\n\n"
    "Things I can do: search the web, send photos, draw images, write and run code, "
    "remember what matters to you, and build new skills (\"make a song skill with this API...\").\n\n"
    "/skills: your custom skills\n/memory: what I remember about you\n"
    "/forget: wipe my memory of you\n/reset: start a fresh conversation (memories stay)\n"
    "/link: get a code to use the same me on the web app\n/link CODE: join a web account"
)


class TelegramBot:
    def __init__(self, agent: Agent, token: str = config.TELEGRAM_BOT_TOKEN):
        self.agent = agent
        self.db = agent.db
        self.app = Application.builder().token(token).concurrent_updates(True).build()
        self.http = httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        self.app.add_handler(CommandHandler(["start", "help"], self.cmd_start))
        self.app.add_handler(CommandHandler("reset", self.cmd_reset))
        self.app.add_handler(CommandHandler("forget", self.cmd_forget))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("skills", self.cmd_skills))
        self.app.add_handler(CommandHandler("link", self.cmd_link))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        me = await self.app.bot.get_me()
        log.info("Telegram bot @%s is running", me.username)

    async def stop(self) -> None:
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        finally:
            await self.http.aclose()

    # --- helpers -----------------------------------------------------------
    def _allowed(self, update: Update) -> bool:
        if not config.TELEGRAM_ALLOWED_USERS:
            return True
        u = update.effective_user
        return bool(u) and (str(u.id) in config.TELEGRAM_ALLOWED_USERS
                            or (u.username or "").lower() in {x.lower().lstrip("@") for x in config.TELEGRAM_ALLOWED_USERS})

    async def _user_id(self, update: Update) -> int:
        u = update.effective_user
        return await self.db.get_or_create_user("telegram", str(u.id), u.first_name or u.username or "")

    async def _send_reply(self, update: Update, reply: Reply) -> None:
        chat = update.effective_chat
        for part in split_message(reply.text):
            try:
                await chat.send_message(md_to_telegram_html(part), parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)
            except TelegramError:
                await chat.send_message(part)
        for att in reply.attachments[:8]:
            await self._send_attachment(update, att)

    async def _load(self, url: str) -> tuple[bytes, str] | None:
        path = media.local_path(url)
        if path:
            return path.read_bytes(), path.name
        try:
            r = await self.http.get(url)
            r.raise_for_status()
            name = url.split("?")[0].rsplit("/", 1)[-1] or "file"
            return r.content, name
        except httpx.HTTPError as exc:
            log.warning("download failed %s: %s", url, exc)
            return None

    async def _send_attachment(self, update: Update, att: dict) -> None:
        chat = update.effective_chat
        url, kind = att.get("url", ""), att.get("type", "file")
        caption = (att.get("caption") or att.get("title") or "")[:1000] or None
        loaded = await self._load(url)
        if not loaded:
            await chat.send_message(f"{caption or 'Link'}: {media.absolute_url(url)}")
            return
        data, name = loaded
        if len(data) > 49 * 1024 * 1024:
            await chat.send_message(f"That file is too big for Telegram, grab it here: {media.absolute_url(url)}")
            return
        f = io.BytesIO(data)
        f.name = name
        try:
            if kind == "image":
                await chat.send_action(ChatAction.UPLOAD_PHOTO)
                await chat.send_photo(f, caption=caption)
            elif kind == "audio":
                await chat.send_action(ChatAction.UPLOAD_VOICE)
                await chat.send_audio(f, caption=caption, title=att.get("title") or None)
            elif kind == "video":
                await chat.send_action(ChatAction.UPLOAD_VIDEO)
                await chat.send_video(f, caption=caption)
            else:
                await chat.send_document(f, caption=caption)
        except TelegramError as exc:
            log.warning("send %s failed: %s", kind, exc)
            await chat.send_message(f"{caption or kind}: {media.absolute_url(url)}")

    async def _chat(self, update: Update, text: str) -> None:
        await update.effective_chat.send_action(ChatAction.TYPING)
        uid = await self._user_id(update)
        reply = await self.agent.respond(uid, text, channel="telegram")
        await self._send_reply(update, reply)

    # --- handlers ------------------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._chat(update, update.message.text)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        voice = update.message.voice or update.message.audio
        file = await voice.get_file()
        data = bytes(await file.download_as_bytearray())
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            text = await self.agent.ai.transcribe(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("transcribe failed: %s", exc)
            text = ""
        if not text:
            await update.message.reply_text("I couldn't hear that clearly, can you type it? 🙈")
            return
        await self._chat(update, f"(voice note) {text}")

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        data = bytes(await file.download_as_bytearray())
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            description = await self.agent.ai.describe_image(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("vision failed: %s", exc)
            description = "(could not see the image)"
        caption = update.message.caption or ""
        await self._chat(update, f"[I sent you a photo. It shows: {description}] {caption}".strip())

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._allowed(update):
            await self._user_id(update)
            await update.message.reply_text(HELP.format(bot=config.BOT_NAME))
        else:
            await update.message.reply_text("Sorry, this bot is private.")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._allowed(update):
            await self.db.archive_history(await self._user_id(update))
            await update.message.reply_text("Fresh start! I still remember the important stuff about you. 🙂")

    async def cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._allowed(update):
            uid = await self._user_id(update)
            await self.db.clear_memories(uid)
            await self.db.archive_history(uid)
            await update.message.reply_text("Okay, I've forgotten everything I knew about you.")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        items = await self.db.list_memories(await self._user_id(update))
        text = "\n".join(f"• {m['content']}" for m in items[:40]) or "I don't know much about you yet, tell me!"
        await update.message.reply_text(text)

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        items = await self.db.list_skills(await self._user_id(update))
        text = "\n".join(f"• {s['name']}: {s['description']}" for s in items) or (
            "No skills yet. Try: \"make a skill that tells crypto prices using the CoinGecko API\"")
        await update.message.reply_text(text)

    async def cmd_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        tg_id = str(update.effective_user.id)
        if context.args:
            target = await self.db.redeem_link_code(context.args[0], "telegram", tg_id)
            msg = "Linked! Telegram and web now share the same memories. 🤝" if target else "That code is invalid or expired."
            await update.message.reply_text(msg)
            return
        uid = await self._user_id(update)
        code = pysecrets.token_hex(3).upper()
        await self.db.create_link_code(uid, code)
        await update.message.reply_text(
            f"Your link code: {code}\nOpen the web app, open the side panel, and enter it under 'Link Telegram'. "
            "It is valid for 15 minutes.")
