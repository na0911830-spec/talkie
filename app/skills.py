"""Self-building skills: the bot writes, tests and stores its own tools."""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import TYPE_CHECKING

from . import sandbox

if TYPE_CHECKING:  # avoid importing httpx for pure helpers and tests
    from .ai import WorkersAI

log = logging.getLogger("dost.skills")

SKILL_SPEC = '''You write or update a Python "skill" (a tool) for a chat bot. Output EXACTLY two fenced blocks and nothing else.

Block 1, ```json: {"description": "<one sentence: what it does and when to use it>",
  "parameters": <JSON Schema object for the arguments the chat model will pass>,
  "test_args": <example arguments for a quick self-test to verify the APIs and code work end-to-end>}

Block 2, ```python: the code. Rules:
- Define `def run(args: dict, secrets: dict, ctx) -> dict`.
- CRITICAL: When the user provides specific API URLs, endpoints, query parameters, and fallback logic, YOU MUST IMPLEMENT EXACTLY THOSE APIS AND ENDPOINTS. Never replace them with generic demos (e.g. Deezer, Wikipedia) or placeholders.
- API keys or secrets the user gave are in `secrets` (use secrets.get("KEY_NAME", "default_if_any")).
- Use only the standard library and `requests` (always pass timeout=30).
- `ctx.out_dir` is a writable folder. Save downloads there (e.g. os.path.join(ctx.out_dir, "filename.ext")).
- Return a dict: {"text": "<short human summary>", "images": [url_or_path], "audio": [url_or_path or {"url":..., "title":...}], "video": [...], "files": [...]}. Omit empty keys.
- Check downloaded media files exist and are non-empty before returning them.
- Handle errors gracefully and raise Exception with clear diagnostic messages if requests or parsing fail.
- Keep it concise, maintainable, and robust. No input(), no infinite loops.
'''

FENCE_RE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.S)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return (slug or "skill")[:40]


def tool_name(skill_name: str) -> str:
    return f"skill_{skill_name}"


def extract_blocks(text: str) -> tuple[dict, str]:
    meta: dict = {}
    code = ""
    for lang, body in FENCE_RE.findall(text or ""):
        lang = (lang or "").lower()
        if lang == "json" and not meta:
            try:
                meta = json.loads(body)
            except json.JSONDecodeError:
                meta = {}
        elif lang in ("python", "py") and not code:
            code = body.strip()
    if not code and "def run" in (text or ""):
        code = text[text.index("def run"):].strip()
        # pull imports that appear before run()
        head = "\n".join(ln for ln in text[: text.index("def run")].splitlines()
                         if ln.startswith(("import ", "from ")))
        code = (head + "\n\n" + code).strip()
    return meta, code


FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket", "pty", "posix",
    "commands", "builtins", "__builtin__", "importlib", "signal",
    "multiprocessing", "threading", "ctypes", "code", "pdb",
    "webbrowser", "winreg", "_winapi", "asyncio.subprocess",
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "getattr",
    "setattr", "delattr", "globals", "locals", "vars", "breakpoint",
}

FORBIDDEN_ATTRS = {
    "__subclasses__", "__bases__", "__mro__", "__globals__",
    "__builtins__", "__class__", "__code__",
}


def check_ast_safety(tree: ast.AST) -> str | None:
    """Strictly forbid dangerous operations, host access, shell execution, or server exploitation."""
    for node in ast.walk(tree):
        # 1. Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg in FORBIDDEN_MODULES:
                    return f"Importing '{alias.name}' is strictly forbidden for security reasons."
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                return f"Importing from '{node.module}' is strictly forbidden for security reasons."

        # 2. Check function calls
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                return f"Calling '{node.func.id}()' is strictly forbidden for security reasons."

        # 3. Check dangerous dunder access (e.g. obj.__class__.__subclasses__)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                return f"Accessing attribute '{node.attr}' is strictly forbidden for security reasons."

    return None


def validate_code(code: str) -> str | None:
    """Return an error message, or None when the code is acceptable."""
    if not code:
        return "no code produced"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    has_run = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run" for n in tree.body)
    if not has_run:
        return "code must define a top-level function run(args, secrets, ctx)"

    safety_err = check_ast_safety(tree)
    if safety_err:
        return safety_err

    return None


def normalise_parameters(params: object) -> dict:
    if not isinstance(params, dict) or params.get("type") != "object":
        props = params if isinstance(params, dict) else {}
        params = {"type": "object", "properties": props}
    params.setdefault("properties", {})
    return params


def as_tool(skill: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool_name(skill["name"]),
            "description": f"[User skill] {skill['description']}"[:900],
            "parameters": normalise_parameters(skill.get("parameters") or {}),
        },
    }


async def generate_skill(ai: "WorkersAI", *, name: str, request: str, secrets: dict,
                         existing_code: str = "", model: str | None = None, attempts: int = 3) -> dict:
    """Ask the model for skill code, self-test it, and retry with the error on failure."""
    secret_names = ", ".join(sorted(secrets)) or "(none)"
    user_prompt = (f"Skill name: {name}\nWhat the user wants:\n{request}\n\n"
                   f"Available secret keys (values are hidden): {secret_names}")
    if existing_code:
        user_prompt += f"\n\nExisting code to update/improve:\n```python\n{existing_code}\n```"
    feedback = ""
    last: dict = {}
    for attempt in range(1, attempts + 1):
        text = await ai.complete(SKILL_SPEC, user_prompt + feedback, max_tokens=2500, temperature=0.2, model=model)
        meta, code = extract_blocks(text)
        err = validate_code(code)
        last = {"name": name, "description": str(meta.get("description") or request[:200]),
                "parameters": normalise_parameters(meta.get("parameters") or {}),
                "code": code, "tested": False, "test_output": "", "attempts": attempt}
        if err:
            feedback = f"\n\nYour previous answer was invalid: {err}. Follow the format exactly."
            continue
        test_args = meta.get("test_args")
        if not isinstance(test_args, dict):
            return last
        outcome = await sandbox.run_skill(code, test_args, secrets, timeout=60)
        if outcome.ok:
            last.update(tested=True, test_output=str(outcome.result.get("text", ""))[:500])
            return last
        log.info("skill %s self-test failed (attempt %s): %s", name, attempt, outcome.error)
        feedback = (f"\n\nYour previous code:\n```python\n{code}\n```\nfailed the self-test with args "
                    f"{json.dumps(test_args)}:\n{outcome.error}\n{outcome.traceback[-1200:]}\nFix it.")
        last["test_output"] = f"self-test failed: {outcome.error}"
    if validate_code(last.get("code", "")):
        raise ValueError("Could not generate valid skill code; try describing the API in more detail.")
    return last
