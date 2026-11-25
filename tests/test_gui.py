"""The GUI server: security checks, streaming chat through each gateway type, deep thinking,
Memory, show-sent data, tools, stop, uploads and conversation management."""

import base64
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from helpers import Sandbox

from controller.config import Config
from controller.context import Context
from controller import gui, i18n

QUESTION = "ما عاصمة العراق؟"


class GuiServerTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self._saved_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.sb.env)
        i18n.set_language("en")
        self.ctx = Context(Config())
        self.app = gui.GuiApp(self.ctx, self.sb.project)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(self.app))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.app.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        if not getattr(self, "_shut", False):
            self.httpd.shutdown()
        self.httpd.server_close()
        self.app.shutdown()
        os.environ.clear()
        os.environ.update(self._saved_env)
        self.sb.cleanup()

    # -------------------------------------------------------------- helpers
    def call(self, path, body=None, method=None, token=True, host=None, timeout=60):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method or ("POST" if body is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Allinone-Token", self.app.token)
        if host:
            req.add_header("Host", host)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def chat(self, **payload):
        payload.setdefault("project", str(self.sb.project))
        status, raw = self.call("/api/chat", payload)
        self.assertEqual(status, 200, raw)
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    @staticmethod
    def of(events, kind):
        return [e for e in events if e["type"] == kind]

    # ---------------------------------------------------------------- tests
    def test_security(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 401)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)
        status, page = self.call("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn("التفكير بعمق", page.decode("utf-8"))
        status, raw = self.call("/api/state")
        state = json.loads(raw)
        names = [g["name"] for g in state["gateways"]]
        self.assertEqual(names[:3], ["omniroute", "freellmapi", "claude"])
        claude = next(g for g in state["gateways"] if g["name"] == "claude")
        self.assertEqual(claude["type"], "anthropic")
        self.assertIn("[ينقل السؤال هنا]", state["deep_template"])

    def test_deep_thinking_memory_and_resume(self):
        ev = self.chat(text=QUESTION, gateway="fake", deep=True)
        conv_id = self.of(ev, "conversation")[0]["id"]
        sent = self.of(ev, "start")[0]["sent"]
        self.assertTrue(sent.startswith("اقرأ سؤالي وحدد النتيجة"))
        self.assertTrue(sent.endswith("سؤالي هو: " + QUESTION))
        self.assertNotIn("[ينقل السؤال هنا]", sent)
        text = "".join(e["text"] for e in self.of(ev, "delta"))
        self.assertIn("echo:", text)
        self.assertEqual(self.of(ev, "result")[0]["session_id"], "sess-stream-1")

        msgs = self.ctx.store().messages(conv_id)
        self.assertEqual(msgs[0]["display"], QUESTION)  # the user only sees the question
        self.assertEqual(msgs[0]["sent"], sent)  # the full text is kept for "show sent text"
        self.assertTrue(msgs[0]["meta"]["deep"])
        self.assertIn("echo:", msgs[1]["display"])

        ev2 = self.chat(text="وماذا عن البصرة؟", gateway="fake", memory=True, conversation_id=conv_id)
        sent2 = self.of(ev2, "start")[0]["sent"]
        self.assertIn(QUESTION, sent2)               # earlier question is in the transcript
        self.assertIn(msgs[1]["display"], sent2)     # and the earlier answer
        self.assertTrue(sent2.rstrip().endswith("وماذا عن البصرة؟"))
        transcript = self.ctx.data_dir / "conversations" / f"conversation-{conv_id}.txt"
        self.assertTrue(transcript.exists())
        self.assertIn(QUESTION, transcript.read_text(encoding="utf-8"))
        self.assertIn(str(transcript), sent2)
        rec = self.sb.read_record()
        self.assertEqual(rec["argv"][rec["argv"].index("--resume") + 1], "sess-stream-1")
        self.assertEqual(self.ctx.store().messages(conv_id)[2]["display"], "وماذا عن البصرة؟")

    def test_claude_account_option(self):
        ev = self.chat(text="hi", gateway="claude")
        err = self.of(ev, "error")
        self.assertTrue(err and err[0].get("need_login"), ev)

        os.environ["FAKE_AUTH"] = "1"
        status, raw = self.call("/api/auth")
        self.assertTrue(json.loads(raw)["loggedIn"])
        first = self.chat(text="first via omni-fake", gateway="fake")
        conv_id = self.of(first, "conversation")[0]["id"]
        ev = self.chat(text="hello claude", gateway="claude", conversation_id=conv_id)
        self.assertTrue(self.of(ev, "notice"), "switching gateway should explain the new session")
        self.assertIn("claude-direct: hello claude", "".join(e["text"] for e in self.of(ev, "delta")))
        rec = self.sb.read_record()
        self.assertNotIn("ANTHROPIC_BASE_URL", rec["env"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", rec["env"])
        self.assertNotIn("--resume", rec["argv"])
        self.assertTrue(rec["env"]["CLAUDE_CONFIG_DIR"].endswith("claude-home"))

    def test_tools_effort_and_permissions(self):
        ev = self.chat(text="please USE_TOOL now", gateway="fake", effort="high", permission_mode="plan",
                       model="fake/model-2")
        use = self.of(ev, "tool_use")[0]
        self.assertEqual(use["name"], "Write")
        self.assertEqual(self.of(ev, "tool_result")[0]["content"], "File created")
        saved = self.of(ev, "saved")[0]["meta"]
        tool = next(p for p in saved["parts"] if p["kind"] == "tool")
        self.assertEqual(tool["result"], "File created")
        self.assertEqual(saved["model"], "fake/model-2")
        argv = self.sb.read_record()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(self.sb.read_record()["env"]["ANTHROPIC_MODEL"], "fake/model-2")

    def _chat_async(self, **payload):
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("events", self.chat(**payload)))
        th.start()
        return th, box

    def _wait_pending(self, count=1):
        for _ in range(600):
            with self.app.lock:
                items = list(self.app.pending.values())
            if len(items) >= count:
                return items
            time.sleep(0.1)
        self.fail("no approval request arrived")

    def test_permission_requests_reach_the_page(self):
        th, box = self._chat_async(text="run NEED_APPROVAL", gateway="fake", permission_mode="acceptEdits")
        item = self._wait_pending()[0]
        self.assertEqual(item["tool"], "Bash")
        self.assertEqual(json.loads(self.call("/api/approval", {"id": item["id"], "allow": True})[1])["ok"], True)
        th.join(90)
        ev = box["events"]
        self.assertEqual(self.of(ev, "approval")[0]["input"], {"command": "npm test 0"})
        self.assertEqual(self.of(ev, "approval_done")[0]["behavior"], "allow")
        self.assertEqual(self.of(ev, "tool_result")[0]["content"], "tests passed")
        argv = self.sb.read_record()["argv"]
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "mcp__allinone_approval__approve")
        conv_id = self.of(ev, "conversation")[0]["id"]

        # deny
        th, box = self._chat_async(text="again NEED_APPROVAL", gateway="fake", conversation_id=conv_id)
        item = self._wait_pending()[0]
        self.call("/api/approval", {"id": item["id"], "allow": False})
        th.join(90)
        res = self.of(box["events"], "tool_result")[0]
        self.assertTrue(res["is_error"])
        self.assertIn("denied", res["content"])

        # "always in this chat": the second request of the same tool is answered without asking
        th, box = self._chat_async(text="NEED_APPROVAL NEED_APPROVAL", gateway="fake", conversation_id=conv_id)
        item = self._wait_pending()[0]
        self.call("/api/approval", {"id": item["id"], "allow": True, "scope": "conversation"})
        th.join(90)
        ev = box["events"]
        self.assertEqual(len(self.of(ev, "approval")), 1)
        self.assertEqual([r["content"] for r in self.of(ev, "tool_result")], ["tests passed", "tests passed"])

    def test_bypass_mode_has_no_prompt_tool(self):
        ev = self.chat(text="hi", gateway="fake", permission_mode="bypassPermissions")
        self.assertTrue(self.of(ev, "result"))
        self.assertNotIn("--permission-prompt-tool", self.sb.read_record()["argv"])

    def test_stop_while_waiting_for_permission(self):
        th, box = self._chat_async(text="NEED_APPROVAL", gateway="fake")
        self._wait_pending()
        with self.app.lock:
            run_id = next(iter(self.app.runs))
        self.call("/api/stop", {"run_id": run_id})
        th.join(60)
        self.assertFalse(th.is_alive())
        self.assertTrue(self.of(box["events"], "saved")[0]["meta"]["stopped"])

    def test_stop(self):
        result = {}

        def run():
            result["events"] = self.chat(text="SLOW job", gateway="fake")

        th = threading.Thread(target=run)
        th.start()
        run_id = None
        for _ in range(600):  # slow CI machines (macOS runners) need a while to start everything
            time.sleep(0.2)
            with self.app.lock:
                if self.app.runs:
                    run_id = next(iter(self.app.runs))
                    break
        self.assertIsNotNone(run_id)
        time.sleep(1.0)
        started = time.time()
        self.assertTrue(json.loads(self.call("/api/stop", {"run_id": run_id})[1])["ok"])
        th.join(60)
        self.assertLess(time.time() - started, 60)
        saved = self.of(result["events"], "saved")[0]["meta"]
        self.assertTrue(saved["stopped"])

    def upload(self, name, data):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/upload?name=" + urllib.request.quote(name),
                                     data=data, method="POST")
        req.add_header("X-Allinone-Token", self.app.token)
        req.add_header("Content-Type", "application/octet-stream")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_upload_limits(self):
        big = os.urandom(3 * 1024 * 1024)
        status, data = self.upload("big.bin", big)
        self.assertEqual(status, 200)
        self.assertEqual(Path(data["path"]).read_bytes(), big)  # raw bytes, nothing re-encoded
        self.app.ctx.cfg.data.setdefault("gui", {})["max_upload_mb"] = 1
        status, data = self.upload("فيديو.mp4", big)  # the server answers cleanly instead of dropping the connection
        self.assertEqual(status, 413)
        self.assertIn("فيديو.mp4", data["error"])
        self.assertEqual(self.call("/api/state")[0], 200)  # still healthy afterwards

    def test_gateway_power_and_shutdown(self):
        ok = json.loads(self.call("/api/gateway", {"name": "fake", "action": "start"})[1])
        self.assertTrue(ok["ok"])
        state = json.loads(self.call("/api/state")[1])
        fake = next(g for g in state["gateways"] if g["name"] == "fake")
        self.assertTrue(fake["up"] and fake["managed"] and fake["can_start"])
        claude = next(g for g in state["gateways"] if g["name"] == "claude")
        self.assertFalse(claude["can_start"])
        res = json.loads(self.call("/api/gateway", {"name": "fake", "action": "stop"})[1])
        self.assertTrue(res["ok"])
        self.assertFalse(next(g for g in json.loads(self.call("/api/state")[1])["gateways"] if g["name"] == "fake")["up"])
        self.app.httpd = self.httpd
        self.assertEqual(self.call("/api/shutdown", {})[0], 200)
        for _ in range(50):
            time.sleep(0.1)
            try:
                self.call("/api/state", timeout=1)
            except OSError:
                break
        else:
            self.fail("server did not shut down")
        self._shut = True  # already stopped by /api/shutdown

    def test_uploads_and_conversations(self):
        status, data = self.upload("../../evil name.txt", "نص مرفق".encode())
        path = Path(data["path"])
        self.assertTrue(path.is_file())
        self.assertIn(str(self.ctx.data_dir / "uploads"), str(path))
        self.assertNotIn("..", path.name)
        ev = self.chat(text="summarise", gateway="fake", attachments=[str(path)])
        self.assertIn(str(path), self.of(ev, "start")[0]["sent"])
        conv_id = self.of(ev, "conversation")[0]["id"]

        convs = json.loads(self.call("/api/conversations")[1])["conversations"]
        self.assertEqual(convs[0]["id"], conv_id)
        self.call(f"/api/conversations/{conv_id}/rename", {"title": "ملخص"})
        self.assertEqual(json.loads(self.call(f"/api/conversations/{conv_id}")[1])["conversation"]["title"], "ملخص")
        found = json.loads(self.call("/api/conversations?q=" + urllib.request.quote("summarise"))[1])["conversations"]
        self.assertEqual(len(found), 1)
        status, md = self.call(f"/api/conversations/{conv_id}/export")
        self.assertIn("# ملخص", md.decode("utf-8"))
        self.assertEqual(json.loads(self.call("/api/memory", {"text": "remember this"})[1])["id"], 1)
        self.call(f"/api/conversations/{conv_id}", method="DELETE")
        self.assertEqual(json.loads(self.call("/api/conversations")[1])["conversations"], [])

    def test_project_validation(self):
        status, raw = self.call("/api/project", {"path": str(self.sb.tmp / "missing")})
        self.assertEqual(status, 400)
        status, raw = self.call("/api/project", {"path": str(self.sb.project)})
        self.assertEqual(json.loads(raw)["path"], str(self.sb.project.resolve()))
        ev = self.chat(text="x", gateway="fake", project=str(self.sb.tmp / "missing"))
        self.assertTrue(self.of(ev, "error"))


class TranslatorTests(unittest.TestCase):
    def test_no_duplicate_text_when_streaming(self):
        tr = gui.StreamTranslator()
        events = [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "sub"}]}, "parent_tool_use_id": "x"},
        ]
        out = [o for e in events for o in tr.feed(e)]
        self.assertEqual([o["type"] for o in out], ["segment", "delta", "delta"])
        self.assertEqual(tr.final_text(), "Hello")

    def test_non_streaming_gateway_text_is_kept(self):
        tr = gui.StreamTranslator()
        tr.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "Whole answer"}]}})
        tr.feed({"type": "result", "is_error": False, "result": "Whole answer", "session_id": "s"})
        self.assertEqual(tr.final_text(), "Whole answer")
        self.assertEqual(tr.session_id, "s")

    def test_deep_template_without_placeholder(self):
        self.assertEqual(gui.apply_deep("Think hard.", "Q?"), "Think hard.\n\nQ?")


if __name__ == "__main__":
    unittest.main()
