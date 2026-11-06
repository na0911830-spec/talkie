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
        self.groq_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self.nvidia_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def close(self) -> None:
        await self.client.aclose()
        await self.groq_client.aclose()
        await self.nvidia_client.aclose()

    async def chat_nvidia(self, messages: list[dict], tools: list[dict] | None = None, *,
                          max_tokens: int = 1024, temperature: float = 0.6,
                          model: str = config.NVIDIA_CHAT_MODEL, retries: int = 2) -> ChatResult:
        if not config.NVIDIA_API_KEY:
            raise AIError("NVIDIA_API_KEY not configured")
        url = f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if "120b" in model.lower() or "super-vl" in model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        for attempt in range(retries):
            try:
                resp = await self.nvidia_client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    known = {t["function"]["name"] for t in tools or []}
                    parsed = parse_chat_response(data, known)
                    parsed.model = model
                    return parsed
                elif resp.status_code in (429, 503):
                    import asyncio
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                else:
                    body = resp.text[:300]
                    raise AIError(f"NVIDIA API error {resp.status_code}: {body}")
            except Exception as e:
                if attempt == retries - 1:
                    raise AIError(f"NVIDIA NIM error: {e}") from e

        raise AIError(f"NVIDIA NIM model {model} failed")

    async def chat_stream_nvidia(self, messages: list[dict], *,
                                max_tokens: int = 1024, temperature: float = 0.6,
                                model: str = config.NVIDIA_CHAT_MODEL):
        if not config.NVIDIA_API_KEY:
            raise AIError("NVIDIA_API_KEY not configured")
        url = f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if "120b" in model.lower() or "super-vl" in model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        import json as pyjson
        async with self.nvidia_client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise AIError(f"NVIDIA stream error {resp.status_code}: {body.decode(errors='replace')}")
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = pyjson.loads(data_str)
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        yield token
                except Exception:
                    continue

    async def chat_groq(self, messages: list[dict], tools: list[dict] | None = None, *,
                        max_tokens: int = config.GROQ_MAX_TOKENS, temperature: float = 0.6,
                        top_p: float = 0.95, model: str = config.GROQ_MODEL, retries: int = 3) -> ChatResult:
        if not config.GROQ_API_KEY:
            raise AIError("GROQ_API_KEY not configured")
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "top_p": top_p,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        for attempt in range(retries):
            resp = await self.groq_client.post(url, json=payload)
            if resp.status_code == 429:
                # Rate limit on Groq free tier: parse retry-after if provided or wait 4-6s
                wait_sec = 4.0 * (attempt + 1)
                log.info("Groq 429 rate limit hit, waiting %.1fs (attempt %d/%d)...", wait_sec, attempt + 1, retries)
                import asyncio
                await asyncio.sleep(wait_sec)
                continue
            if resp.status_code == 400 and "tool" in resp.text and tools:
                # If Groq returns tool_use_failed with failed_generation XML, parse it!
                try:
                    err_json = resp.json().get("error", {})
                    failed_gen = err_json.get("failed_generation", "")
                    if failed_gen:
                        known = {t["function"]["name"] for t in tools}
                        _, recovered_tools = parse_text_tool_calls(failed_gen, known)
                        if recovered_tools:
                            return ChatResult(content="", tool_calls=recovered_tools, model=model)
                except Exception as e:
                    log.warning("Could not recover failed_generation tool call: %s", e)

                log.warning("Groq tool error, retrying without tools: %s", resp.text)
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                continue
            if resp.status_code != 200:
                raise AIError(f"Groq API error {resp.status_code}: {resp.text}")
            break
        else:
            raise AIError(f"Groq API rate limit exceeded after {retries} retries.")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        raw_tools = msg.get("tool_calls") or []
        parsed_tools: list[ToolCall] = []
        for tc in raw_tools:
            fn = tc.get("function") or {}
            parsed_tools.append(ToolCall(
                id=tc.get("id") or "",
                name=fn.get("name") or "",
                arguments=decode_arguments(fn.get("arguments") or {}),
            ))
        content = msg.get("content") or ""
        # Also check for text-embedded tool calls if any
        if not parsed_tools and tools:
            known = {t["function"]["name"] for t in tools}
            content, parsed_tools = parse_text_tool_calls(content, known)
        return ChatResult(content=content, tool_calls=parsed_tools, model=model)

    async def chat_stream_groq(self, messages: list[dict], *,
                               max_tokens: int = config.GROQ_MAX_TOKENS, temperature: float = 0.6,
                               top_p: float = 0.95, model: str = config.GROQ_MODEL, retries: int = 3):
        """Async generator yielding chunks of text from Groq streaming."""
        if not config.GROQ_API_KEY:
            raise AIError("GROQ_API_KEY not configured")
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "top_p": top_p,
            "stream": True,
        }
        import asyncio
        import json as pyjson

        for attempt in range(retries):
            try:
                async with self.groq_client.stream("POST", url, json=payload) as resp:
                    if resp.status_code == 429:
                        log.info("Groq stream 429 rate limit, backing off (attempt %d/%d)...", attempt + 1, retries)
                        await asyncio.sleep(2.5 * (attempt + 1))
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise AIError(f"Groq stream error {resp.status_code}: {body.decode(errors='replace')}")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = pyjson.loads(data_str)
                            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                            token = delta.get("content")
                            if token:
                                yield token
                        except Exception:
                            continue
                    return
            except (httpx.HTTPError, AIError) as e:
                if attempt == retries - 1:
                    raise

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

        # 1. Primary: NVIDIA NIM (Nemotron 3.5 Bolt E4B)
        if not models and config.NVIDIA_API_KEY:
            try:
                return await self.chat_nvidia(messages, tools, max_tokens=max_tokens, temperature=temperature)
            except Exception as exc:
                log.warning("NVIDIA NIM chat failed, falling back to Groq: %s", exc)

        # 2. Fallback 1: Groq (Qwen 3.8 27B)
        if not models and config.GROQ_API_KEY:
            try:
                return await self.chat_groq(messages, tools, max_tokens=max_tokens, temperature=temperature)
            except Exception as exc:
                log.warning("Groq chat failed, falling back to Workers AI: %s", exc)

        # 3. Fallback 2: Cloudflare Workers AI
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
        # For coding/completion, prioritize NVIDIA Code Model (Nemotron 3.5 Super VL 120B)
        target_model = model or config.CODE_MODEL
        if config.NVIDIA_API_KEY and "nvidia" in target_model.lower():
            try:
                res = await self.chat_nvidia(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens, temperature=temperature, model=target_model
                )
                return res.content
            except Exception as exc:
                log.warning("NVIDIA code completion failed, falling back: %s", exc)

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
        # 1. Try Cloudflare image model (e.g. @cf/black-forest-labs/flux-2-dev or flux-1-schnell)
        try:
            model = config.IMAGE_MODEL
            # flux-2-dev expects multipart form data
            if "flux-2-dev" in model:
                url = self.base + model
                form = {"prompt": prompt[:2000]}
                resp = await self.client.post(url, data=form, timeout=60.0)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    if "image" in content_type:
                        return resp.content
                    data = resp.json()
                    b64 = (data.get("result") or {}).get("image") or data.get("image")
                    if b64:
                        return base64.b64decode(b64)
            else:
                result = await self.run_json(model, {"prompt": prompt[:2000], "steps": steps})
                b64 = result.get("image")
                if b64:
                    return base64.b64decode(b64)
        except Exception as exc:
            log.warning("Cloudflare image gen failed (%s), trying fast generation fallback...", exc)

        # 2. High-quality fallback image generation (Pollinations Flux)
        try:
            import urllib.parse
            encoded = urllib.parse.quote(prompt[:300])
            url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&nologo=true"
            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.get(url)
                if res.status_code == 200 and len(res.content) > 1000:
                    return res.content
        except Exception as exc:
            log.error("Image generation fallback failed: %s", exc)

        raise AIError("Unable to generate image right now.")

    async def transcribe(self, audio: bytes, filename: str = "audio.m4a") -> str:
        # 1. Try Groq Whisper (fast & accurate)
        if config.GROQ_API_KEY:
            try:
                # Groq strictly requires one of: [flac, mp3, mp4, mpeg, mpga, m4a, ogg, opus, wav, webm]
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "m4a"
                if ext in ("oga", "opus"):
                    ext = "ogg"
                elif ext not in ("flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"):
                    ext = "ogg"
                safe_filename = f"audio.{ext}"
                mime = "audio/ogg" if ext == "ogg" else "audio/wav" if ext == "wav" else "audio/mp4" if ext == "m4a" else "audio/mpeg"

                files = {"file": (safe_filename, audio, mime)}
                data = {
                    "model": config.STT_MODEL if config.STT_MODEL.startswith("whisper") else "whisper-large-v3-turbo",
                    "temperature": "0",
                    "response_format": "verbose_json",
                }
                resp = await self.groq_client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    files=files,
                    data=data,
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    text = resp.json().get("text", "").strip()
                    if text:
                        return text
                else:
                    log.warning("Groq whisper error status=%d: %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                log.warning("Groq whisper transcription failed: %s", exc)

        # 2. Fallback to Cloudflare Whisper
        try:
            cf_stt = getattr(config, "CF_STT_MODEL", "@cf/openai/whisper")
            resp = await self.run(cf_stt, content=audio)
            res_data = resp.json()
            if res_data.get("success"):
                return (res_data.get("result") or {}).get("text", "").strip()
        except Exception as exc:
            log.warning("Cloudflare whisper failed: %s", exc)

        return ""

    async def generate_speech(self, text: str) -> bytes | None:
        """Generate audio bytes for text using Cloudflare TTS (with high quality male voice fallback)."""
        clean_text = text.strip()
        if not clean_text:
            return None

        # 1. Try Cloudflare TTS (e.g. @cf/deepgram/aura-2-en with male speaker)
        tts_model = getattr(config, "TTS_MODEL", "@cf/deepgram/aura-2-en")
        try:
            if "aura" in tts_model:
                payload = {
                    "text": clean_text[:2000],
                    "speaker": getattr(config, "TTS_SPEAKER", "orion"),
                    "encoding": "mp3",
                }
            else:
                payload = {"prompt": clean_text[:500]}

            resp = await self.run(tts_model, payload=payload)
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "application/json" not in content_type and len(resp.content) > 100:
                return resp.content
            # If JSON returned, check if audio or error
            try:
                data = resp.json()
                if data.get("result", {}).get("audio"):
                    import base64
                    return base64.b64decode(data["result"]["audio"])
            except Exception:
                pass
        except Exception as exc:
            log.warning("Cloudflare TTS failed: %s", exc)

        # 2. Try Edge-TTS realistic male neural voice
        try:
            import edge_tts
            # Detect whether text is Hindi / Hinglish or English
            is_hindi = any("\u0900" <= c <= "\u097f" for c in clean_text) or any(
                w in clean_text.lower().split() for w in ["bhai", "yaar", "kya", "haal", "kaise", "chal", "suno", "mast", "badhiya"]
            )
            voice = "hi-IN-MadhurNeural" if is_hindi else "en-IN-PrabhatNeural"
            communicate = edge_tts.Communicate(clean_text[:600], voice=voice, rate="+5%")
            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])
            if audio_chunks:
                return b"".join(audio_chunks)
        except Exception as exc:
            log.warning("Edge TTS male voice fallback failed: %s", exc)

        # 3. Fallback to Google TTS
        try:
            import urllib.parse
            encoded = urllib.parse.quote(clean_text[:300])
            lang = "hi" if any("\u0900" <= c <= "\u097f" for c in clean_text) else "en-in"
            url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl={lang}&client=tw-ob&q={encoded}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200 and len(res.content) > 500:
                    return res.content
        except Exception as exc:
            log.warning("TTS fallback failed: %s", exc)

        return None

    async def describe_image(self, image: bytes, prompt: str = "Describe this image in detail.") -> str:
        # 1. Try NVIDIA NIM Vision Model (nemotron-3.5-super-vl-120b-a12b or configured VISION_MODEL)
        if config.NVIDIA_API_KEY:
            try:
                b64_img = base64.b64encode(image).decode("utf-8")
                # Infer MIME type from magic bytes if possible, default to jpeg
                mime = "image/png" if image.startswith(b"\x89PNG") else "image/webp" if image.startswith(b"RIFF") and b"WEBP" in image[:12] else "image/jpeg"
                url = f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
                payload = {
                    "model": config.VISION_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_img}"}},
                            ],
                        }
                    ],
                    "max_tokens": 1024,
                    "temperature": 0.2,
                }
                resp = await self.nvidia_client.post(url, json=payload, timeout=60.0)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") or []
                    if choices:
                        msg = choices[0].get("message") or {}
                        content = (msg.get("content") or "").strip()
                        if not content and msg.get("reasoning_content"):
                            # If content is empty because reasoning consumed tokens or finish reason
                            content = msg.get("reasoning_content", "").strip()
                        if content:
                            return content
                else:
                    log.warning("NVIDIA NIM vision failed status=%d: %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                log.warning("NVIDIA NIM vision error (%s), trying fallbacks...", exc)

        # 2. Try Cloudflare vision model as fallback
        try:
            cf_model = getattr(config, "CF_VISION_MODEL", "@cf/llava-hf/llava-1.5-7b-hf")
            result = await self.run_json(cf_model, {
                "image": list(image), "prompt": prompt, "max_tokens": 300,
            })
            desc = (result.get("description") or result.get("response") or "").strip()
            if desc:
                return desc
        except Exception as exc:
            log.warning("Cloudflare vision failed: %s", exc)

        return "An image uploaded by the user."
