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


# --- Cloudflare Workers AI (hardcoded defaults) -----------------------------
CF_ACCOUNT_ID = _env("CF_ACCOUNT_ID", "a96e56336d23c37c05cc3d3a6dcf65c8")
CF_API_TOKEN = _env("CF_API_TOKEN", "cfat_iJdd2T3ctOVGhPWR6zKV0h3TLHrO5qmx1pIeesFH42c68a16")
CF_BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/"

# Only models that carry no per-token price in the Workers AI catalog
# (they only draw from the daily free neuron allocation).
CHAT_MODELS = [m.strip() for m in _env(
    "CHAT_MODELS",
    "@cf/ibm-granite/granite-4.0-h-micro,@cf/daily-co/phonellm-mini-alpha-2",
).split(",") if m.strip()]
CODE_MODEL = _env("CODE_MODEL", "@cf/ibm-granite/granite-4.0-h-micro")
EMBED_MODEL = _env("EMBED_MODEL", "@cf/google/embeddinggemma-300m")
EMBED_DIM = int(_env("EMBED_DIM", "768"))
IMAGE_MODEL = _env("IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
STT_MODEL = _env("STT_MODEL", "@cf/openai/whisper")
VISION_MODEL = _env("VISION_MODEL", "@cf/llava-hf/llava-1.5-7b-hf")

# --- App ---------------------------------------------------------------------
DATABASE_URL = _env("DATABASE_URL", "postgresql://dost:dost@localhost:5432/dost")
DATA_DIR = Path(_env("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
MEDIA_DIR = DATA_DIR / "media"
PUBLIC_URL = _env("PUBLIC_URL", "http://localhost:8080").rstrip("/")

BOT_NAME = _env("BOT_NAME", "Dost")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = {u.strip() for u in _env("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}

WEB_PASSWORD = _env("WEB_PASSWORD", "dost123")
SECRET_KEY = _env("SECRET_KEY", "change-this-secret-key-in-production")

ALLOW_CODE_EXEC = _env("ALLOW_CODE_EXEC", "true").lower() == "true"
CODE_TIMEOUT_SEC = int(_env("CODE_TIMEOUT_SEC", "25"))
SKILL_TIMEOUT_SEC = int(_env("SKILL_TIMEOUT_SEC", "120"))

HISTORY_WINDOW = int(_env("HISTORY_WINDOW", "16"))
MAX_TOOL_STEPS = int(_env("MAX_TOOL_STEPS", "4"))
