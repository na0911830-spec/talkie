"""Cloudflare Workers AI client (REST)."""
from __future__ import annotations

import base64
import logging

import httpx

from . import config
from .parsing import (  # noqa: F401 - re-exported
    ChatResult, ToolCall, decode_arguments, parse_chat_response, parse_text_tool_calls)

log = logging.getLogger("dost.ai")


class AIError(RuntimeError):
    pass


class WorkersAI:
    def __init__(self, account_id: str = config.CF_ACCOUNT_ID, token: str = config.CF_API_TOKEN):
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def run(self, model: str, payload: dict | None = None, *, content: bytes | None = None) -> httpx.Response:
        url = self.base + model
        if content is not None:
            resp = await self.client.post(url, content=content)
        else:
            resp = await self.client.post(url, json=payload or {})
        return resp

    async def run_json(self, model: str, payload: dict) -> dict:
        resp = await self.run(model, payload)
        try:
            data = resp.json()
        except ValueError as exc:
            raise AIError(f"{model}: HTTP {resp.status_code}") from exc
        if not data.get("success", False):
            raise AIError(f"{model}: {data.get('errors')}")
        return data.get("result") or {}

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *,
                   max_tokens: int = 900, temperature: float = 0.7,
                   models: list[str] | None = None) -> ChatResult:
        known = {t["function"]["name"] for t in tools or []}
        last_err: Exception | None = None
        for model in models or config.CHAT_MODELS:
            payload: dict = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
            if tools:
                payload["tools"] = tools
            try:
                result = await self.run_json(model, payload)
                parsed = parse_chat_response(result, known)
                parsed.model = model
                return parsed
            except (AIError, httpx.HTTPError) as exc:
                log.warning("chat model %s failed: %s", model, exc)
                last_err = exc
        raise AIError(f"All chat models failed: {last_err}")

    async def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                       temperature: float = 0.3, model: str | None = None) -> str:
        res = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens, temperature=temperature,
            models=[model] + config.CHAT_MODELS if model else None,
        )
        return res.content

    async def embed(self, texts: list[str]) -> list[list[float]]:
        texts = [t[:4000] or " " for t in texts]
        result = await self.run_json(config.EMBED_MODEL, {"text": texts})
        return result.get("data") or []

    async def generate_image(self, prompt: str, steps: int = 4) -> bytes:
        result = await self.run_json(config.IMAGE_MODEL, {"prompt": prompt[:2000], "steps": steps})
        b64 = result.get("image")
        if not b64:
            raise AIError("image model returned no image")
        return base64.b64decode(b64)

    async def transcribe(self, audio: bytes) -> str:
        resp = await self.run(config.STT_MODEL, content=audio)
        data = resp.json()
        if not data.get("success"):
            raise AIError(f"transcribe: {data.get('errors')}")
        return (data.get("result") or {}).get("text", "").strip()

    async def describe_image(self, image: bytes, prompt: str = "Describe this image in detail.") -> str:
        result = await self.run_json(config.VISION_MODEL, {
            "image": list(image), "prompt": prompt, "max_tokens": 300,
        })
        return (result.get("description") or result.get("response") or "").strip()
