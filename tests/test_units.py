"""Unit tests. Standard library only, so they run with unittest or pytest."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config, sandbox, skills
from app.parsing import (clean_answer, decode_arguments, history_to_messages, language_hint,
                         parse_chat_response, parse_fact_list)
from app.textfmt import md_to_telegram_html, split_message


class ParsingTests(unittest.TestCase):
    def test_decode_arguments_handles_double_encoding(self):
        self.assertEqual(decode_arguments('"{\\n \\"query\\": \\"Delhi weather\\"\\n}"'), {"query": "Delhi weather"})
        self.assertEqual(decode_arguments({"a": 1}), {"a": 1})
        self.assertEqual(decode_arguments("plain words"), {"input": "plain words"})
        self.assertEqual(decode_arguments(""), {})

    def test_parse_openai_style_tool_call(self):
        result = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "x"}'}}]}}]}
        parsed = parse_chat_response(result, {"web_search"})
        self.assertEqual(parsed.content, "")
        self.assertEqual(parsed.tool_calls[0].name, "web_search")
        self.assertEqual(parsed.tool_calls[0].arguments, {"query": "x"})

    def test_parse_text_tool_call_fallback(self):
        result = {"response": '<tool_call>{"name": "generate_image", "arguments": {"prompt": "cat"}}</tool_call>'}
        parsed = parse_chat_response(result, {"generate_image"})
        self.assertEqual(parsed.tool_calls[0].arguments, {"prompt": "cat"})
        self.assertEqual(parsed.content, "")

    def test_parse_plain_reply_strips_think(self):
        parsed = parse_chat_response({"response": "<think>hmm</think>Haan bhai!"}, set())
        self.assertEqual(parsed.content, "Haan bhai!")
        self.assertFalse(parsed.tool_calls)

    def test_history_merges_same_role(self):
        rows = [{"role": "user", "content": "hi"}, {"role": "user", "content": "there"},
                {"role": "assistant", "content": "hello", "attachments": [{"type": "image"}]}]
        msgs = history_to_messages(rows)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["content"], "hi\nthere")
        self.assertIn("[sent: image]", msgs[1]["content"])

    def test_language_hint_and_cleaning(self):
        self.assertIn("Hinglish", language_hint("yaar aaj mood off hai"))
        self.assertIn("Devanagari", language_hint("मुझे आज अकेला लग रहा है"))
        self.assertIn("English", language_hint("What is the weather in Pune?"))
        self.assertIn("same language", language_hint("¿Cómo estás, amigo? 你好"))
        self.assertEqual(clean_answer("Enjoy!\n![a](http://x/y.jpg)\n**Preview URL:** http://fake", True), "Enjoy!")

    def test_parse_fact_list(self):
        self.assertEqual(parse_fact_list('Sure: ["Name is Riya", "Loves chai"]'), ["Name is Riya", "Loves chai"])
        self.assertEqual(parse_fact_list("nothing"), [])

    def test_telegram_html_and_split(self):
        html = md_to_telegram_html("**Hi** <b> `x<y`\n```py\nprint('<a>')\n```")
        self.assertIn("<b>Hi</b>", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("<code>x&lt;y</code>", html)
        self.assertIn("print('&lt;a&gt;')", html)
        parts = split_message("```\n" + "a\n\n" * 3000 + "```", limit=1000)
        self.assertTrue(all(p.count("```") % 2 == 0 for p in parts))


class SkillTests(unittest.TestCase):
    def test_block_extraction_and_validation(self):
        text = ('```json\n{"description": "Plays songs", "parameters": {"query": {"type": "string"}}, '
                '"test_args": null}\n```\n'
                '```python\nimport requests\n\ndef run(args, secrets, ctx):\n    return {"text": "ok"}\n```')
        meta, code = skills.extract_blocks(text)
        self.assertEqual(meta["description"], "Plays songs")
        self.assertIsNone(skills.validate_code(code))
        self.assertEqual(skills.normalise_parameters(meta["parameters"])["type"], "object")
        self.assertIsNotNone(skills.validate_code("def nope(): pass"))
        self.assertTrue(skills.validate_code("def run(:").startswith("SyntaxError"))
        self.assertEqual(skills.slugify("Song Player!"), "song_player")

    def test_as_tool_prefixes_name(self):
        tool = skills.as_tool({"name": "song_player", "description": "Plays songs", "parameters": {}})
        self.assertEqual(tool["function"]["name"], "skill_song_player")
        self.assertEqual(tool["function"]["parameters"]["type"], "object")


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [mock.patch.object(config, "DATA_DIR", tmp),
                        mock.patch.object(config, "MEDIA_DIR", tmp / "media")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_skill_runs_and_collects_files(self):
        code = '''
import os
def run(args, secrets, ctx):
    print("noise that must not break the protocol")
    p = os.path.join(ctx.out_dir, "song.mp3")
    open(p, "wb").write(b"ID3fake")
    return {"text": f"Playing {args['q']} with key {secrets['API_KEY']}",
            "audio": [{"path": "song.mp3", "title": "Kesariya"}],
            "images": ["https://example.com/cover.jpg", "/etc/passwd"]}
'''
        out = asyncio.run(sandbox.run_skill(code, {"q": "kesariya"}, {"API_KEY": "k1"}))
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.result["text"], "Playing kesariya with key k1")
        kinds = {a["type"]: a for a in out.attachments}
        self.assertTrue(kinds["audio"]["url"].startswith("/media/"))
        self.assertEqual(kinds["audio"]["title"], "Kesariya")
        self.assertEqual(kinds["image"]["url"], "https://example.com/cover.jpg")
        self.assertEqual(len(out.attachments), 2)  # /etc/passwd refused

    def test_skill_errors_reported_and_env_hidden(self):
        with mock.patch.dict(os.environ, {"CF_API_TOKEN": "should-not-leak"}):
            out = asyncio.run(sandbox.run_skill(
                "import os\ndef run(a, s, c):\n    raise ValueError(os.environ.get('CF_API_TOKEN', 'hidden'))", {}, {}))
        self.assertFalse(out.ok)
        self.assertIn("hidden", out.error)

    def test_run_python_output_and_timeout(self):
        out = asyncio.run(sandbox.run_python("print(6*7)"))
        self.assertTrue(out.ok)
        self.assertEqual(out.stdout.strip(), "42")
        out = asyncio.run(sandbox.run_python("while True: pass", timeout=2))
        self.assertFalse(out.ok)
        self.assertIn("Timed out", out.error)


if __name__ == "__main__":
    unittest.main()
