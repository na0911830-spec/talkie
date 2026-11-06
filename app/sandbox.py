"""Run skill code and user code snippets in isolated subprocesses."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, media
from .skill_runner import RESULT_MARKER

ROOT = Path(__file__).resolve().parent.parent


def _limits() -> None:  # pragma: no cover - runs in the child
    try:
        import resource
        # CPU limit: max 15 seconds CPU time to prevent infinite loops or cryptominers
        resource.setrlimit(resource.RLIMIT_CPU, (15, 20))
        # Address space (RAM) limit: 300MB max to prevent memory exhaustion
        resource.setrlimit(resource.RLIMIT_AS, (300 * 1024 * 1024, 300 * 1024 * 1024))
        # File size limit: 50MB max file writes
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        # Process / thread count limit: prevent fork bombs
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (30, 30))
        os.setsid()
    except Exception:
        pass


def _child_env() -> dict:
    # Never leak the bot's own credentials into user code.
    keep = {"PATH", "LANG", "LC_ALL", "HOME", "TZ", "SSL_CERT_FILE", "PYTHONIOENCODING"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass
class RunOutcome:
    ok: bool
    result: dict = field(default_factory=dict)
    error: str = ""
    traceback: str = ""
    stdout: str = ""
    attachments: list[dict] = field(default_factory=list)


async def _communicate(cmd: list[str], stdin: bytes, cwd: str, timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=_child_env(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=_limits,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -9, "", f"Timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _collect_attachments(result: dict, out_dir: Path) -> list[dict]:
    """Turn images/audio/video/files entries (URLs or local paths) into attachments."""
    attachments: list[dict] = []
    for key, kind in (("images", "image"), ("audio", "audio"), ("video", "video"), ("files", "file")):
        items = result.get(key) or []
        if isinstance(items, (str, dict)):
            items = [items]
        for item in items:
            caption = ""
            title = ""
            if isinstance(item, dict):
                caption = str(item.get("caption", ""))
                title = str(item.get("title", ""))
                item = item.get("url") or item.get("path") or ""
            item = str(item)
            if not item:
                continue
            if item.startswith(("http://", "https://")):
                url = item
            else:
                path = Path(item)
                if not path.is_absolute():
                    path = out_dir / path
                try:
                    path.resolve().relative_to(out_dir.resolve())
                except ValueError:
                    continue  # refuse files outside the job directory
                if not path.is_file():
                    continue
                url = media.adopt_file(path)
            actual_kind = kind if kind != "file" else media.kind_for(url, "file")
            attachments.append({"type": actual_kind, "url": url, "caption": caption, "title": title})
    return attachments


async def run_skill(code: str, args: dict, secrets: dict, *, user_id: int | None = None,
                    timeout: int | None = None) -> RunOutcome:
    out_dir = Path(tempfile.mkdtemp(prefix="skill_", dir=_work_root()))
    try:
        job = json.dumps({"code": code, "args": args, "secrets": secrets,
                          "out_dir": str(out_dir), "user_id": user_id}).encode()
        rc, out, err = await _communicate(
            [sys.executable, "-m", "app.skill_runner"], job, str(out_dir), timeout or config.SKILL_TIMEOUT_SEC)
        line = next((ln for ln in out.splitlines() if ln.startswith(RESULT_MARKER)), None)
        if line is None:
            return RunOutcome(ok=False, error=(err.strip()[-1500:] or f"skill process exited with {rc}"))
        data = json.loads(line[len(RESULT_MARKER):])
        if not data.get("ok"):
            return RunOutcome(ok=False, error=data.get("error", ""), traceback=data.get("traceback", ""),
                              stdout=data.get("stdout", ""))
        result = data.get("result") or {}
        return RunOutcome(ok=True, result=result, stdout=data.get("stdout", ""),
                          attachments=_collect_attachments(result, out_dir))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


async def run_python(code: str, timeout: int | None = None) -> RunOutcome:
    """Execute a plain Python snippet and capture its output."""
    out_dir = Path(tempfile.mkdtemp(prefix="code_", dir=_work_root()))
    try:
        script = out_dir / "main.py"
        script.write_text(code, encoding="utf-8")
        rc, out, err = await _communicate(
            [sys.executable, str(script)], b"", str(out_dir), timeout or config.CODE_TIMEOUT_SEC)
        files = [p for p in out_dir.iterdir() if p.is_file() and p.name != "main.py"]
        attachments = [{"type": media.kind_for(p.name), "url": media.adopt_file(p), "caption": p.name}
                       for p in files[:5]]
        return RunOutcome(ok=rc == 0, stdout=out[-4000:], error=err[-3000:], attachments=attachments)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _work_root() -> str:
    root = config.DATA_DIR / "work"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)
