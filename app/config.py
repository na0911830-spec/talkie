"""Central configuration.

Cloudflare credentials are hardcoded as defaults on purpose (requested by the
owner). Every value can still be overridden with an environment variable.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


# --- NVIDIA NIM API ---------------------------------------------------------
NVIDIA_API_KEY = _env("NVIDIA_API_KEY", "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")
NVIDIA_BASE_URL = _env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_CHAT_MODEL = _env("NVIDIA_CHAT_MODEL", "nvidia/nemotron-3.5-bolt-e4b")
NVIDIA_CODE_MODEL = _env("NVIDIA_CODE_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")

# --- Cloudflare Workers AI (hardcoded defaults) -----------------------------
CF_ACCOUNT_ID = _env("CF_ACCOUNT_ID", "a96e56336d23c37c05cc3d3a6dcf65c8")
CF_API_TOKEN = _env("CF_API_TOKEN", "cfat_Boj2TJCpX8UnBrjciEZEmfluM5Cx2C0lgadgI0XVf8ddf17e")
CF_BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/"

# Fast, intelligent conversational models fallback
GROQ_API_KEY = _env("GROQ_API_KEY", "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")
GROQ_MODEL = _env("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_MAX_TOKENS = int(_env("GROQ_MAX_TOKENS", "1024"))

CHAT_MODELS = [m.strip() for m in _env(
    "CHAT_MODELS",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/qwen/qwen3.8-27b,@cf/meta/llama-3.1-8b-instruct-fp8",
).split(",") if m.strip()]
CODE_MODEL = _env("CODE_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
EMBED_MODEL = _env("EMBED_MODEL", "@cf/google/embeddinggemma-300m")
EMBED_DIM = int(_env("EMBED_DIM", "768"))
IMAGE_MODEL = _env("IMAGE_MODEL", "@cf/black-forest-labs/flux-2-dev")
STT_MODEL = _env("STT_MODEL", "whisper-large-v3-turbo")
CF_STT_MODEL = _env("CF_STT_MODEL", "@cf/openai/whisper")
TTS_MODEL = _env("TTS_MODEL", "@cf/deepgram/aura-2-en")
TTS_SPEAKER = _env("TTS_SPEAKER", "orion")
VISION_MODEL = _env("VISION_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
CF_VISION_MODEL = _env("CF_VISION_MODEL", "@cf/llava-hf/llava-1.5-7b-hf")

# --- App ---------------------------------------------------------------------
DATABASE_URL = _env("DATABASE_URL", "postgresql://dost:dost@localhost:5432/dost")
DATA_DIR = Path(_env("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
MEDIA_DIR = DATA_DIR / "media"
PUBLIC_URL = _env("PUBLIC_URL", "http://localhost:8080").rstrip("/")

BOT_NAME = _env("BOT_NAME", "Talkie")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "8944367104:AAHx37a1ISmX-CCEpwM4yJPwWHIS9VdHr3A")
TELEGRAM_ALLOWED_USERS = {u.strip() for u in _env("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}

WEB_PASSWORD = _env("WEB_PASSWORD", "dost123")
SECRET_KEY = _env("SECRET_KEY", "change-this-secret-key-in-production")

ALLOW_CODE_EXEC = _env("ALLOW_CODE_EXEC", "true").lower() == "true"
CODE_TIMEOUT_SEC = int(_env("CODE_TIMEOUT_SEC", "25"))
SKILL_TIMEOUT_SEC = int(_env("SKILL_TIMEOUT_SEC", "120"))

HISTORY_WINDOW = int(_env("HISTORY_WINDOW", "8"))
MAX_TOOL_STEPS = int(_env("MAX_TOOL_STEPS", "4"))
