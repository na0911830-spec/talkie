"""Subprocess entrypoint that executes one skill or code snippet.

Reads a JSON job from stdin: {"code", "args", "secrets", "out_dir"}.
The skill code must define ``run(args, secrets, ctx)`` (sync or async).
Writes exactly one JSON line prefixed with RESULT_MARKER to stdout.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import sys
import traceback
from types import SimpleNamespace

RESULT_MARKER = "__DOST_RESULT__"


def main() -> None:
    job = json.loads(sys.stdin.read())
    real_stdout = sys.stdout
    captured = io.StringIO()
    result: dict
    try:
        namespace: dict = {"__name__": "dost_skill"}
        with contextlib.redirect_stdout(captured):
            exec(compile(job["code"], "<skill>", "exec"), namespace)
            fn = namespace.get("run")
            if not callable(fn):
                raise RuntimeError("skill code must define run(args, secrets, ctx)")
            ctx = SimpleNamespace(out_dir=job.get("out_dir", "."), user_id=job.get("user_id"))
            value = fn(job.get("args") or {}, job.get("secrets") or {}, ctx)
            if inspect.isawaitable(value):
                value = asyncio.run(value)
        if value is None:
            value = {}
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict):
            value = {"text": json.dumps(value, default=str)[:6000]}
        result = {"ok": True, "result": value, "stdout": captured.getvalue()[-4000:]}
    except BaseException as exc:  # noqa: BLE001 - report everything back
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-3000:],
            "stdout": captured.getvalue()[-2000:],
        }
    real_stdout.write(RESULT_MARKER + json.dumps(result, default=str) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
