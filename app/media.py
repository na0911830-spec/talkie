"""Local media store for generated images, downloaded songs and files."""
from __future__ import annotations

import mimetypes
import shutil
import uuid
from pathlib import Path

from . import config

AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}


def ensure_dirs() -> None:
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def kind_for(name: str, default: str = "file") -> str:
    ext = Path(name.split("?")[0]).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return default


def save_bytes(data: bytes, ext: str) -> str:
    ensure_dirs()
    name = f"{uuid.uuid4().hex}{ext if ext.startswith('.') else '.' + ext}"
    (config.MEDIA_DIR / name).write_bytes(data)
    return f"/media/{name}"


def adopt_file(path: Path) -> str:
    """Move a file produced by a skill into the media store."""
    ensure_dirs()
    safe = "".join(ch for ch in path.name if ch.isalnum() or ch in "._- ")[-80:] or "file"
    name = f"{uuid.uuid4().hex[:10]}_{safe}"
    shutil.move(str(path), config.MEDIA_DIR / name)
    return f"/media/{name}"


def local_path(url: str) -> Path | None:
    if not url.startswith("/media/"):
        return None
    name = Path(url).name
    path = config.MEDIA_DIR / name
    return path if path.is_file() else None


def absolute_url(url: str) -> str:
    return config.PUBLIC_URL + url if url.startswith("/") else url


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
