"""Telegram front-end (long polling) with full auth, media, skills & contact management."""
from __future__ import annotations

import asyncio
import io
import logging
import secrets as pysecrets
import typing

import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import auth, config, media
from .agent import Agent, Reply
from .textfmt import md_to_telegram_html, split_message

log = logging.getLogger("dost.telegram")

HELP = (
    "Hey! I'm <b>{bot}</b>, your real-life best friend (dost/yaar/buddy). Chat with me in any language!\n\n"
    "✨ <b>What I can do</b>:\n"
    "• Chat naturally in Hinglish, English, Hindi, etc.\n"
    "• Search the web, fetch news, weather &amp; facts\n"
    "• Play music and songs (Muzo skill) and send playable audio\n"
    "• Send photos, generate AI images, describe sent pictures\n"
    "• Voice notes support: send me voice, I listen and respond\n"
    "• Build and execute custom skills\n"
    "• Set reminders (e.g. 'remind me to stand up at 5pm')\n"
    "• Remember facts about you\n\n"
    "⚙️ <b>Commands</b>:\n"
    "• /newchat or /reset - Start a fresh chat session (keeps your long-term memories)\n"
    "• /reminders - View your active reminders\n"
    "• /skills - View your custom skills\n"
    "• /memory - View what I know about you\n"
    "• /forget - Wipe all stored memories and history\n"
    "• /link - Link with your web account\n"
    "• /setpassword &lt;new_password&gt; - Update your account password\n"
    "• /help - Show this guide"
)


class TelegramBot:
    def __init__(self, agent: Agent, token: str = config.TELEGRAM_BOT_TOKEN):
        self.agent = agent
        self.db = agent.db
        from telegram.request import HTTPXRequest
        req = HTTPXRequest(connection_pool_size=16, read_timeout=120.0, write_timeout=120.0, connect_timeout=30.0)
        self.app = Application.builder().token(token).request(req).concurrent_updates(True).build()
        self.http = httpx.AsyncClient(timeout=120, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        # In-memory set of authenticated telegram IDs that persists across chats & restarts
        self._authenticated_users: set[int] = set()
        self._reminder_task: asyncio.Task | None = None

        # Command handlers
        self.app.add_handler(CommandHandler(["start", "help"], self.cmd_start))
        self.app.add_handler(CommandHandler(["newchat", "reset"], self.cmd_newchat))
        self.app.add_handler(CommandHandler("forget", self.cmd_forget))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("skills", self.cmd_skills))
        self.app.add_handler(CommandHandler(["reminders", "reminder"], self.cmd_reminders))
        self.app.add_handler(CommandHandler("link", self.cmd_link))
        self.app.add_handler(CommandHandler("setpassword", self.cmd_set_password))

        # Media & document handlers
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        self._reminder_task = asyncio.create_task(self._reminder_worker())
        me = await self.app.bot.get_me()
        log.info("Telegram bot @%s is running", me.username)

    async def stop(self) -> None:
        if self._reminder_task:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        finally:
            await self.http.aclose()

    async def _reminder_worker(self) -> None:
        """Background loop to periodically deliver due reminders."""
        log.info("Reminder background worker started")
        while True:
            try:
                due = await self.db.get_due_reminders()
                for rem in due:
                    rem_id = rem["id"]
                    user_id = rem["user_id"]
                    chat_id = rem.get("chat_id")
                    text = rem.get("reminder_text", "")
                    channel = rem.get("channel", "telegram")

                    if channel == "telegram" or not chat_id:
                        # Find telegram chat id if not present
                        if not chat_id:
                            identity = await self.db.pool.fetchrow(
                                "SELECT external_id FROM identities WHERE user_id=$1 AND channel='telegram'", user_id)
                            if identity:
                                chat_id = identity["external_id"]

                    if chat_id:
                        msg = (
                            f"⏰ <b>Reminder!</b>\n\n"
                            f"Hey, you asked me to remind you:\n"
                            f"👉 <b>{text}</b>\n\n"
                            f"Ho gaya ya abhi kar raha hai? 😊"
                        )
                        try:
                            await self.app.bot.send_message(
                                chat_id=chat_id,
                                text=msg,
                                parse_mode=ParseMode.HTML
                            )
                            log.info("Delivered reminder #%d to user %s (chat %s)", rem_id, user_id, chat_id)
                            # Add assistant message to history so AI remembers having sent the reminder
                            await self.db.add_message(user_id, "assistant", f"[Delivered reminder: {text}]", "telegram")
                        except Exception as exc:
                            log.warning("Failed to send reminder #%d: %s", rem_id, exc)

                    await self.db.mark_reminder_completed(rem_id, status="completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Error in reminder worker loop: %s", e)

            await asyncio.sleep(10)

    # --- auth & access guard -----------------------------------------------
    def _allowed(self, update: Update) -> bool:
        if not config.TELEGRAM_ALLOWED_USERS:
            return True
        u = update.effective_user
        return bool(u) and (
            str(u.id) in config.TELEGRAM_ALLOWED_USERS
            or (u.username or "").lower() in {x.lower().lstrip("@") for x in config.TELEGRAM_ALLOWED_USERS}
        )

    async def _authenticate_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
        """Check user authentication state.
        
        Extracts display_name and username from Telegram.
        If user exists and has a password:
          - If logged in for this session: returns user_id.
          - Else: prompts for password to sign in.
        If user is new or has no password set:
          - Prompts to set a password to sign up.
        """
        tg_user = update.effective_user
        if not tg_user:
            return None

        tg_id = str(tg_user.id)
        u_data = context.user_data

        # Grab full name & username from Telegram profile
        first = tg_user.first_name or ""
        last = tg_user.last_name or ""
        full_name = f"{first} {last}".strip() or tg_user.username or "Friend"
        username = tg_user.username or f"tg_{tg_id}"

        # Check existing db user for this telegram identity
        user = await self.db.get_user_by_identity("telegram", tg_id)

        if not user:
            # Check if an existing account with the same username exists (e.g. from web)
            existing_by_uname = await self.db.get_user_by_username(username)
            if existing_by_uname:
                user = existing_by_uname
                await self.db.link_identity(user["id"], "telegram", tg_id)
            else:
                # New account
                uid = await self.db.get_or_create_user("telegram", tg_id, full_name)
                user = await self.db.get_user(uid)

        uid = user["id"]
        # Make sure user profile has their telegram name and username recorded
        await self.db.update_user_profile(uid, username=username, display_name=full_name)

        # Check if already authenticated (in database, in-memory set, or in user_data)
        if user.get("authenticated_telegram") or uid in self._authenticated_users or u_data.get("authenticated_user_id") == uid:
            self._authenticated_users.add(uid)
            u_data["authenticated_user_id"] = uid
            return uid

        # If user has no password set yet (sign up)
        if not user.get("password_hash"):
            u_data["auth_state"] = "awaiting_new_password"
            u_data["pending_uid"] = uid
            return None

        # User has a password but not authenticated yet (sign in)
        u_data["auth_state"] = "awaiting_login_password"
        u_data["pending_uid"] = uid
        return None

    # --- replies & media sending -------------------------------------------
    async def _send_reply(self, update: Update, reply: Reply, is_voice: bool = False) -> None:
        chat = update.effective_chat
        msg_id = update.message.message_id if update.message else None

        # If user sent a voice message, reply with a voice note instead of text
        if is_voice and reply.text and reply.text.strip():
            try:
                await chat.send_action(ChatAction.RECORD_VOICE)
                # Strip Markdown artifacts/code blocks for clean speech
                speech_text = reply.text
                # Remove code blocks and markdown formatting symbols
                import re
                speech_text = re.sub(r"```[\s\S]*?```", "", speech_text)
                speech_text = re.sub(r"[*_`~#]", "", speech_text).strip()
                if speech_text:
                    voice_bytes = await self.agent.ai.generate_speech(speech_text)
                    if voice_bytes:
                        await chat.send_action(ChatAction.UPLOAD_VOICE)
                        f = io.BytesIO(voice_bytes)
                        f.name = "voice.mp3"
                        # Send as telegram voice message
                        await chat.send_voice(
                            voice=f,
                            caption=None,
                            reply_to_message_id=msg_id,
                        )
                        for att in reply.attachments[:8]:
                            await self._send_attachment(update, att)
                        return
            except Exception as exc:
                log.warning("failed to send voice reply, falling back to text: %s", exc)

        first_msg = True
        for part in split_message(reply.text):
            reply_to = msg_id if first_msg else None
            first_msg = False
            try:
                await chat.send_message(
                    md_to_telegram_html(part),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_to_message_id=reply_to,
                )
            except TelegramError:
                await chat.send_message(part, reply_to_message_id=reply_to)

        for att in reply.attachments[:8]:
            await self._send_attachment(update, att)

    def _determine_reaction(self, user_text: str, reply: Reply) -> str | None:
        """Pick a fitting reaction emoji for natural conversation flow, or None."""
        text_lower = user_text.lower()
        reply_lower = (reply.text or "").lower()

        # 1. Music / Audio played or requested
        if reply.attachments and any(a.get("type") == "audio" for a in reply.attachments):
            return "🔥"
        if any(w in text_lower for w in ["play ", "gaana", "gana", "song", "audio", "flac"]):
            return "🔥"

        # 2. Jokes / Laughing / Roasts / Memes
        funny_patterns = [
            "haha", "hehe", "lmao", "lol", "rofl", "😂", "🤣", "xd",
            "berozgar", "baap", "pagal", "chutiya", "kya baat", "funny"
        ]
        if any(w in text_lower for w in funny_patterns) or ("😂" in reply_lower or "haha" in reply_lower):
            return "😂"

        # 3. Appreciation / Affection / Friendship / Compliments
        love_patterns = ["love", "bhai tu mast", "dost", "pyaar", "dil jeet", "shukriya", "thank", "thanks"]
        if any(w in text_lower for w in love_patterns):
            return "❤️"

        # 4. Agreement / Cool / Confirmation / Vibes
        cool_patterns = ["badhiya", "mast", "sahi", "solid", "awesome", "great", "nice", "chill", "perfect"]
        if any(w in text_lower for w in cool_patterns):
            return "👍"

        # 5. Mind blown / Shock
        shock_patterns = ["wtf", "omg", "kya yaar", "shock", "impossible", "sach me", "seriously"]
        if any(w in text_lower for w in shock_patterns):
            return "🤯"

        return None

    async def _maybe_react(self, update: Update, text: str, reply: Reply) -> None:
        """Selectively react to messages where appropriate to feel like a real friend."""
        if not update.message:
            return
        emoji = self._determine_reaction(text, reply)
        if not emoji:
            return
        try:
            chat = update.effective_chat
            if chat and update.message.message_id:
                await self.app.bot.set_message_reaction(
                    chat_id=chat.id,
                    message_id=update.message.message_id,
                    reaction=emoji,
                )
        except Exception as exc:
            log.debug("could not set reaction: %s", exc)

    async def _load(self, url: str) -> tuple[bytes, str] | None:
        path = media.local_path(url)
        if path and path.is_file():
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
        url = att.get("url", "")
        kind = att.get("type", "file")
        caption = (att.get("caption") or att.get("title") or "")[:1000] or None
        loaded = await self._load(url)
        if not loaded:
            log.warning("could not load media attachment %s", url)
            return

        data, name = loaded
        if len(data) > 49 * 1024 * 1024:
            log.warning("file is too large to send on telegram: %d bytes", len(data))
            return

        f = io.BytesIO(data)
        f.name = name
        try:
            if kind == "image":
                await chat.send_action(ChatAction.UPLOAD_PHOTO)
                await chat.send_photo(f, caption=caption)
            elif kind == "audio":
                await chat.send_action(ChatAction.UPLOAD_VOICE)
                await chat.send_audio(
                    f,
                    caption=caption,
                    title=att.get("title") or name,
                    performer=config.BOT_NAME,
                )
            elif kind == "video":
                await chat.send_action(ChatAction.UPLOAD_VIDEO)
                await chat.send_video(f, caption=caption)
            else:
                await chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                await chat.send_document(f, caption=caption)
        except TelegramError as exc:
            log.warning("send %s failed: %s", kind, exc)

    async def _chat(self, update: Update, text: str, uid: int, is_voice: bool = False) -> None:
        if is_voice:
            await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        else:
            await update.effective_chat.send_action(ChatAction.TYPING)
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""

        status_msg: typing.Any = None

        async def _notify_status(msg_text: str) -> None:
            nonlocal status_msg
            try:
                if update.message:
                    status_msg = await update.message.reply_text(
                        msg_text,
                        reply_to_message_id=update.message.message_id,
                    )
            except Exception as e:
                log.debug("could not send status msg: %s", e)

        reply = await self.agent.respond(uid, text, channel="telegram", chat_id=chat_id, status_callback=_notify_status)

        # Delete status message if it was sent so reply is clean
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        await self._send_reply(update, reply, is_voice=is_voice)
        await self._maybe_react(update, text, reply)

    # --- message handlers --------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        u_data = context.user_data
        text = (update.message.text or "").strip()

        # Handle pending auth states first
        auth_state = u_data.get("auth_state")
        pending_uid = u_data.get("pending_uid")

        if auth_state == "awaiting_login_password":
            user = await self.db.get_user(pending_uid)
            if user and auth.verify_password(text, user.get("password_hash", "")):
                await self.db.set_telegram_authenticated(pending_uid, True)
                self._authenticated_users.add(pending_uid)
                u_data["authenticated_user_id"] = pending_uid
                u_data.pop("auth_state", None)
                u_data.pop("pending_uid", None)
                await update.message.reply_text(
                    f"Welcome back, {user.get('display_name') or 'friend'}! Password verified. 🔓\n\nHow's it going?"
                )
            else:
                await update.message.reply_text("❌ Incorrect password. Please try again:")
            return

        if auth_state == "awaiting_new_password":
            if len(text) < 4:
                await update.message.reply_text("Please choose a password with at least 4 characters:")
                return
            pw_hash = auth.hash_password(text)
            await self.db.update_user_password(pending_uid, pw_hash)
            await self.db.set_telegram_authenticated(pending_uid, True)
            self._authenticated_users.add(pending_uid)
            u_data["authenticated_user_id"] = pending_uid
            u_data.pop("auth_state", None)
            u_data.pop("pending_uid", None)
            user = await self.db.get_user(pending_uid)
            name = user.get("display_name") or "friend"
            await update.message.reply_text(
                f"Password set successfully! 🎉 Welcome, {name}.\n\nYou can now chat freely or use /help to see all features."
            )
            return

        # Check normal authentication
        uid = await self._authenticate_user(update, context)
        if not uid:
            state = u_data.get("auth_state")
            if state == "awaiting_login_password":
                await update.message.reply_text("🔒 Please enter your password to unlock your chat:")
            else:
                await update.message.reply_text("👋 Welcome! Please enter a secure password to set up your account:")
            return

        await self._chat(update, text, uid)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        voice = update.message.voice or update.message.audio
        file = await voice.get_file()
        data = bytes(await file.download_as_bytearray())
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        try:
            text = await self.agent.ai.transcribe(data, filename="voice.ogg")
        except Exception as exc:  # noqa: BLE001
            log.warning("transcribe failed: %s", exc)
            text = ""
        if not text:
            await update.message.reply_text("I couldn't hear that clearly, can you type it? 🙈")
            return
        await self._chat(update, f"(voice note: keep your reply very short, casual, and concise, 1-2 sentences max) {text}", uid, is_voice=True)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
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
        await self._chat(update, f"[I sent you a photo. It shows: {description}] {caption}".strip(), uid)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        doc = update.message.document
        if not doc:
            return

        caption = update.message.caption or ""
        fname = doc.file_name or "document"
        await update.effective_chat.send_action(ChatAction.TYPING)
        await self._chat(update, f"[User sent a file: {fname}] {caption}".strip(), uid)

    # --- command handlers ----------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            await update.message.reply_text("Sorry, this bot is private.")
            return

        tg_user = update.effective_user
        first = tg_user.first_name or ""
        last = tg_user.last_name or ""
        full_name = f"{first} {last}".strip() or tg_user.username or "Friend"
        username = tg_user.username or f"tg_{tg_user.id}"

        uid = await self._authenticate_user(update, context)
        if uid:
            msg_id = update.message.message_id
            await update.message.reply_text(
                f"Hey {full_name} (@{username})! You are already signed in.\n\n" + HELP.format(bot=config.BOT_NAME),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg_id,
            )
            return

        state = context.user_data.get("auth_state")
        if state == "awaiting_login_password":
            await update.message.reply_text(
                f"👋 Welcome back, {full_name}! (@{username})\n\n"
                "🔒 **Sign In**: Please reply with your password to continue:"
            )
        else:
            await update.message.reply_text(
                f"👋 Welcome, {full_name}! (@{username})\n\n"
                "✨ **Sign Up**: Please set a password for your account (reply with your desired password):"
            )

    async def cmd_newchat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a fresh chat conversation, archiving previous history while preserving long-term memory."""
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        await self.db.archive_history(uid)
        await update.message.reply_text(
            "✨ *Fresh new chat started!*\n\n"
            "Previous messages are archived so we start clean, but I still remember everything important about you! "
            "Kya chal raha hai bhai?",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_set_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Update or change user password."""
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your current password first:")
            return

        if not context.args:
            await update.message.reply_text("Usage: /setpassword <your_new_password>")
            return

        new_pw = context.args[0].strip()
        if len(new_pw) < 4:
            await update.message.reply_text("Password must be at least 4 characters long.")
            return

        await self.db.update_user_password(uid, auth.hash_password(new_pw))
        await update.message.reply_text("✅ Password updated successfully!")

    async def cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        await self.db.clear_memories(uid)
        await self.db.archive_history(uid)
        await update.message.reply_text("Okay, I've cleared all memories and history. Starting completely fresh.")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        items = await self.db.list_memories(uid)
        text = "\n".join(f"• {m['content']}" for m in items[:40]) or "I don't know much about you yet, tell me something!"
        await update.message.reply_text(text)

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        items = await self.db.list_skills(uid)
        text = "\n".join(f"• {s['name']}: {s['description']}" for s in items) or (
            "No custom skills yet. You can ask me anytime: 'Make a song skill called muzo with this API...'")
        await update.message.reply_text(text)

    async def cmd_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        items = await self.db.list_user_reminders(uid, status="pending")
        if not items:
            await update.message.reply_text("You have no pending reminders! Ask me anytime: 'Remind me to call Mom at 8pm'.")
            return

        from zoneinfo import ZoneInfo
        tz_ist = ZoneInfo("Asia/Kolkata")
        lines = []
        for r in items:
            dt = r["remind_at"].astimezone(tz_ist).strftime("%I:%M %p (%d %b)")
            lines.append(f"⏰ <b>{r['reminder_text']}</b> at {dt}")
        await update.message.reply_text("📋 <b>Your Reminders</b>:\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update) or not update.message:
            return

        tg_id = str(update.effective_user.id)
        if context.args:
            target = await self.db.redeem_link_code(context.args[0], "telegram", tg_id)
            if target:
                context.user_data["authenticated_user_id"] = target
                msg = "Linked successfully! Your Telegram and Web account now share the same memory and skills. 🤝"
            else:
                msg = "That code is invalid or expired."
            await update.message.reply_text(msg)
            return

        uid = await self._authenticate_user(update, context)
        if not uid:
            await update.message.reply_text("🔒 Please enter your password first:")
            return

        code = pysecrets.token_hex(3).upper()
        await self.db.create_link_code(uid, code)
        await update.message.reply_text(
            f"Your link code: `{code}`\n\n"
            "Open the web app side panel and enter it under 'Link Telegram'. Valid for 15 minutes.",
            parse_mode=ParseMode.MARKDOWN,
        )
