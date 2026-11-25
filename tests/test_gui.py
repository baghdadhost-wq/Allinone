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

from helpers import ROOT, Sandbox

try:
    import playwright.sync_api  # noqa: F401
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "claude" / "visual"))
    import browser_lib as _bl
    VISUAL_BROWSER = _bl.find_browser() or ""
    VISUAL_OK = True
except ImportError:
    VISUAL_BROWSER, VISUAL_OK = "", False

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
        self.assertTrue("".join(e["text"] for e in self.of(ev, "delta")).startswith("claude-direct:"))
        sent = self.sb.read_record()["prompt"]
        self.assertIn("ملف تسليم المهمة", sent)              # hand-over note of the earlier work
        self.assertIn("first via omni-fake", sent)
        self.assertTrue(sent.rstrip().endswith("hello claude"))
        rec = self.sb.read_record()
        self.assertNotIn("ANTHROPIC_BASE_URL", rec["env"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", rec["env"])
        self.assertNotIn("--resume", rec["argv"])
        self.assertTrue(rec["env"]["CLAUDE_CONFIG_DIR"].endswith("claude-home"))

    def test_state_carries_display_name(self):
        from controller import i18n
        i18n.set_brand("smarter")
        self.addCleanup(lambda: i18n.set_brand(""))
        st = json.loads(self.call("/api/state")[1])
        self.assertEqual(st["brand"], "smarter")
        ev = self.chat(text="please USE_TOOL", gateway="fake", model="claude-sonnet-4-5")
        conv_id = self.of(ev, "conversation")[0]["id"]
        self.assertEqual(self.sb.read_record()["env"]["ANTHROPIC_MODEL"], "claude-sonnet-4-5")  # the real id is used
        report = json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"]
        self.assertNotIn("Claude", report)
        self.assertNotIn("claude-sonnet", report)
        self.assertIn("smarter-sonnet-4-5", report)

    def test_several_claude_accounts(self):
        os.environ["FAKE_AUTH"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_AUTH", None))
        res = json.loads(self.call("/api/accounts", {"action": "add", "name": "Work Pro"})[1])
        acc = res["id"]
        self.assertEqual(acc, "work-pro")
        self.assertEqual([a["id"] for a in res["accounts"]], ["default", "work-pro"])
        ev = self.chat(text="build it", gateway="claude", account=acc)
        conv_id = self.of(ev, "conversation")[0]["id"]
        rec = self.sb.read_record()
        self.assertTrue(rec["env"]["CLAUDE_CONFIG_DIR"].endswith(str(Path("claude-accounts") / acc)))  # its own sign-in
        self.assertEqual(self.ctx.store().conversation(conv_id)["session_gateway"], f"claude#{acc}")
        self.assertEqual(self.of(ev, "saved")[0]["meta"]["account"], acc)
        self.chat(text="more", gateway="claude", account=acc, conversation_id=conv_id)
        self.assertIn("--resume", self.sb.read_record()["argv"])          # same account: same session
        ev = self.chat(text="continue on the main account", gateway="claude", account="default", conversation_id=conv_id)
        rec = self.sb.read_record()
        self.assertNotIn("--resume", rec["argv"])                         # another account: new session…
        self.assertIn("ملف تسليم المهمة", rec["prompt"])                  # …with the work so far
        self.assertIn("build it", rec["prompt"])
        self.assertTrue(rec["env"]["CLAUDE_CONFIG_DIR"].endswith("claude-home"))
        st = json.loads(self.call(f"/api/auth?account={acc}")[1])
        self.assertEqual(st["account"], acc)
        self.call("/api/accounts", {"action": "remove", "id": acc})
        self.assertEqual([a["id"] for a in json.loads(self.call("/api/accounts")[1])["accounts"]], ["default"])
        self.assertEqual(self.call("/api/accounts", {"action": "remove", "id": "default"})[0], 400)

    def test_claude_ai_by_hand(self):
        proj = self.sb.tmp / "web-proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (proj / "README.md").write_text("# shop\n", encoding="utf-8")
        (proj / "node_modules" / "x").mkdir(parents=True)
        (proj / "node_modules" / "x" / "big.js").write_text("junk", encoding="utf-8")
        status, raw = self.call("/api/bridge/prepare", {"text": "fix app.py to print v2", "project": str(proj)})
        self.assertEqual(status, 200, raw)
        r = json.loads(raw)
        pkg = r["package"]
        conv_id = r["conversation_id"]
        self.assertIn("<<<FILE: المسار/النسبي/للملف.ext>>>", pkg)         # Claude is told how to answer
        self.assertIn("<<<FILE: src/app.py>>>\nprint('v1')", pkg)       # the file it needs
        self.assertIn("src/app.py", r["files"])
        self.assertNotIn("node_modules", pkg)
        self.assertTrue(pkg.rstrip().endswith("fix app.py to print v2"))
        self.assertTrue(Path(r["file"]).is_file())
        answer = ("سأعدّل الملف.\n<<<FILE: src/app.py>>>\n```python\nprint('v2')\n```\n<<<END FILE>>>\n"
                  "<<<FILE: src/util.py>>>\nX = 1\n<<<END FILE>>>\n<<<FILE: ../evil.py>>>\nboom\n<<<END FILE>>>\n"
                  "<<<DELETE: README.md>>>\n<<<RUN: python src/app.py>>>\nجرّبه.")
        prev = json.loads(self.call("/api/bridge/preview", {"conversation_id": conv_id, "answer": answer})[1])
        actions = {x["path"]: x["action"] for x in prev["plan"]}
        self.assertEqual(actions, {"src/app.py": "modify", "src/util.py": "create", "../evil.py": "rejected",
                                   "README.md": "delete"})
        self.assertEqual(prev["runs"], ["python src/app.py"])
        self.app.ctx.cfg.data.setdefault("gui", {})["verify"] = {"command": 'python3 -c "import sys; sys.exit(3)"',
                                                                 "auto_detect": False}
        res = json.loads(self.call("/api/bridge/apply", {"conversation_id": conv_id, "answer": answer, "verify": True,
                                                         "paths": ["src/app.py", "src/util.py", "../evil.py"]})[1])
        self.assertEqual((proj / "src" / "app.py").read_text(), "print('v2')\n")
        self.assertTrue((proj / "src" / "util.py").is_file())
        self.assertFalse((self.sb.tmp / "evil.py").exists())
        self.assertTrue((proj / "README.md").is_file())                   # not chosen: kept
        self.assertEqual(res["meta"]["verify"]["status"], "failed")       # checked here, on your computer
        run = json.loads(self.call("/api/bridge/run", {"conversation_id": conv_id, "command": "python3 src/app.py"})[1])
        self.assertEqual((run["code"], run["output"].strip()), (0, "v2"))
        # the next request carries the work so far, the changed file and the failed check
        nxt = json.loads(self.call("/api/bridge/prepare", {"text": "now add tests", "conversation_id": conv_id})[1])["package"]
        self.assertIn("ملف تسليم المهمة", nxt)
        self.assertIn("✔ src/util.py", nxt)
        self.assertIn("<<<FILE: src/app.py>>>\nprint('v2')", nxt)
        self.assertIn("نتيجة آخر تحقق على جهازي", nxt)
        self.assertIn("نتيجة الأمر python3 src/app.py: نجح", nxt)
        # undo restores the earlier files
        self.call("/api/bridge/undo", {"conversation_id": conv_id, "message_id": res["message_id"]})
        self.assertEqual((proj / "src" / "app.py").read_text(), "print('v1')\n")
        self.assertFalse((proj / "src" / "util.py").exists())
        self.assertEqual(self.call("/api/bridge/undo", {"conversation_id": conv_id, "message_id": res["message_id"]})[0], 400)

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

    def upload(self, name, data, target="/api/upload"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{target}?name=" + urllib.request.quote(name),
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

    # ------------------------------------------------- report / answer / pre-approval / projects
    def test_error_report_and_uploaded_answer(self):
        ev = self.chat(text="please USE_TOOL now", gateway="fake")
        conv_id = self.of(ev, "conversation")[0]["id"]
        th, box = self._chat_async(text="run NEED_APPROVAL", gateway="fake", conversation_id=conv_id)
        item = self._wait_pending()[0]
        self.call("/api/approval", {"id": item["id"], "allow": False})
        th.join(90)

        status, raw = self.call(f"/api/conversations/{conv_id}/report", {})
        self.assertEqual(status, 200, raw)
        rep = json.loads(raw)
        text = rep["text"]
        self.assertTrue(Path(rep["path"]).is_file())
        self.assertIn(str(self.ctx.data_dir / "reports"), rep["path"])
        self.assertEqual(Path(rep["path"]).read_text(encoding="utf-8"), text)
        self.assertIn("أنت خبير برمجيات", text)              # header from config/prompts/report_header.txt
        self.assertIn("please USE_TOOL now", text)           # what was asked
        self.assertIn("hello.txt", text)                     # the tool input
        self.assertIn("File created", text)                  # its result
        self.assertIn("✘", text)                             # the failed step is marked
        self.assertIn("Permission denied", text)
        self.assertIn("[إذن] Bash: رُفض", text)               # the approval decision
        self.assertIn("ملخص الأخطاء (1)", text)
        self.assertEqual(self.call("/api/conversations/999/report", {})[0], 400)
        state = json.loads(self.call("/api/state")[1])
        self.assertIn("تقرير", state["report_ask"])

        answer = "السبب: npm غير مثبت.\nالحل: ثبّت Node ثم أعد npm test."
        ev = self.chat(text="من Claude.ai", gateway="fake", conversation_id=conv_id, expert={"answer": answer})
        sent = self.of(ev, "start")[0]["sent"]
        self.assertIn(answer, sent)
        self.assertIn("مستشار خارجي", sent)
        self.assertIn("ملاحظة المستخدم: من Claude.ai", sent)
        self.assertEqual(self.sb.read_record()["prompt"], sent)
        last_user = [m for m in self.ctx.store().messages(conv_id) if m["role"] == "user"][-1]
        self.assertEqual(last_user["display"], "من Claude.ai")   # the page shows the note, not the long answer
        self.assertTrue(last_user["meta"]["expert"])
        ev = self.chat(text="", gateway="fake", conversation_id=conv_id, expert={"answer": "  "})
        self.assertTrue(self.of(ev, "error"))
        ev = self.chat(gateway="fake", conversation_id=conv_id, expert={"answer": "do X"})
        self.assertIn("📥", self.of(ev, "start")[0]["sent"] and
                      [m for m in self.ctx.store().messages(conv_id) if m["role"] == "user"][-1]["display"])

    def test_preapproval(self):
        th, box = self._chat_async(text="NEED_APPROVAL NEED_APPROVAL", gateway="fake", preapprove=True)
        th.join(90)
        ev = box["events"]
        self.assertEqual(self.of(ev, "approval"), [])          # no permission card at all
        self.assertEqual(len(self.of(ev, "approval_auto")), 2)
        self.assertEqual([r["content"] for r in self.of(ev, "tool_result")], ["tests passed", "tests passed"])
        conv_id = self.of(ev, "conversation")[0]["id"]
        self.assertTrue(json.loads(self.call(f"/api/conversations/{conv_id}")[1])["conversation"]["preapproved"])
        saved = self.of(ev, "saved")[0]["meta"]
        self.assertEqual([a["auto"] for a in saved["approvals"]], [True, True])

        # turned off -> the card comes back; turning it on while a request waits answers it
        self.call("/api/preapprove", {"conversation_id": conv_id, "on": False})
        th, box = self._chat_async(text="NEED_APPROVAL", gateway="fake", conversation_id=conv_id)
        self._wait_pending()
        res = json.loads(self.call("/api/preapprove", {"conversation_id": conv_id, "on": True})[1])
        self.assertEqual(res["answered"], 1)
        th.join(90)
        self.assertEqual(self.of(box["events"], "tool_result")[0]["content"], "tests passed")

        # other conversations are not affected
        th, box = self._chat_async(text="NEED_APPROVAL", gateway="fake")
        item = self._wait_pending()[0]
        self.call("/api/approval", {"id": item["id"], "allow": True})
        th.join(90)
        self.assertEqual(len(self.of(box["events"], "approval")), 1)

    def test_projects(self):
        folder = self.sb.tmp / "proj-folder"
        folder.mkdir()
        status, raw = self.call("/api/projects", {"name": "متجر", "description": "موقع المتجر",
                                                  "instructions": "اكتب الكود بلغة TypeScript دائماً.",
                                                  "folder": str(folder), "gateway": "fake"})
        self.assertEqual(status, 200, raw)
        pid = json.loads(raw)["project"]["id"]
        self.assertEqual(self.call("/api/projects", {"name": "x", "folder": str(self.sb.tmp / "nope")})[0], 400)
        status, f = self.upload("دليل.md", "# الدليل".encode(), target=f"/api/projects/{pid}/files")
        self.assertEqual(status, 200, f)
        self.assertTrue(Path(f["path"]).is_file())
        self.assertEqual(self.upload("a.txt", b"x", target="/api/projects/999/files")[0], 404)

        ev = self.chat(text="ابدأ", gateway="fake", project_id=pid, project="")
        conv_id = self.of(ev, "conversation")[0]["id"]
        rec = self.sb.read_record()
        self.assertEqual(Path(rec["cwd"]).resolve(), folder.resolve())   # the project's folder is used
        argv = rec["argv"]
        prompt_file = Path(argv[argv.index("--append-system-prompt-file") + 1])
        sys_prompt = prompt_file.read_text(encoding="utf-8")
        self.assertIn("اكتب الكود بلغة TypeScript دائماً.", sys_prompt)
        self.assertIn(f["path"], sys_prompt)
        add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str(Path(f["path"]).parent), add_dirs)
        self.assertIn(str(self.ctx.data_dir / "uploads"), add_dirs)

        # later messages stay in the project even if the page does not resend project_id
        self.chat(text="تابع", gateway="fake", conversation_id=conv_id)
        self.assertIn("--append-system-prompt-file", self.sb.read_record()["argv"])
        proj = json.loads(self.call(f"/api/projects/{pid}")[1])["project"]
        self.assertEqual([c["id"] for c in proj["conversations"]], [conv_id])
        self.assertEqual(len(proj["files"]), 1)
        listed = json.loads(self.call("/api/projects")[1])["projects"]
        self.assertEqual((listed[0]["chats"], listed[0]["files"]), (1, 1))
        self.assertEqual(json.loads(self.call(f"/api/conversations?project={pid}")[1])["conversations"][0]["id"], conv_id)
        self.assertEqual(json.loads(self.call(f"/api/conversations/{conv_id}")[1])["conversation"]["project_name"], "متجر")
        self.assertIn("المشروع: متجر", json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"])

        self.call(f"/api/projects/{pid}", {"instructions": "تعليمات جديدة"})
        self.chat(text="مرة أخرى", gateway="fake", conversation_id=conv_id)
        self.assertIn("تعليمات جديدة", prompt_file.read_text(encoding="utf-8"))
        self.call(f"/api/projects/{pid}/files/{f['id']}", method="DELETE")
        self.assertFalse(Path(f["path"]).exists())

        ev = self.chat(text="عادي", gateway="fake")              # chats outside a project get no project prompt
        argv = self.sb.read_record()["argv"]
        general = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8")
        self.assertNotIn("تعليمات جديدة", general)
        self.assertIn("قواعد التشغيل", general)                   # the always-on rules (background servers…)
        self.call(f"/api/projects/{pid}", method="DELETE")
        self.assertEqual(json.loads(self.call("/api/projects")[1])["projects"], [])
        self.assertIsNone(self.ctx.store().conversation(conv_id)["project_id"])  # the chat is kept
        self.assertFalse((self.ctx.data_dir / "projects" / str(pid)).exists())

    # ------------------------------------------------------------- interruptions
    def _fast_resume(self):
        saved = dict(gui._REASONS)
        for k, v in saved.items():
            gui._REASONS[k] = (v[0], v[1], 0, v[3], v[4])
        self.addCleanup(lambda: gui._REASONS.update(saved))

    def test_rate_limit_resumes_automatically_in_same_session(self):
        self._fast_resume()
        ev = self.chat(text="build it FAIL_429", gateway="fake")
        conv_id = self.of(ev, "conversation")[0]["id"]
        saved = self.of(ev, "saved")
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["meta"]["interrupted"]["reason"], "rate_limit")
        self.assertNotIn("interrupted", saved[1]["meta"])
        auto = self.of(ev, "auto_resume")[0]
        self.assertEqual((auto["attempt"], auto["reason"]), (1, "rate_limit"))
        argv = self.sb.read_record()["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-stream-1")   # same Claude Code session
        self.assertIn("انقطع تنفيذ المهمة", self.sb.read_record()["prompt"])
        users = [m for m in self.ctx.store().messages(conv_id) if m["role"] == "user"]
        self.assertEqual([u["display"] for u in users], ["build it FAIL_429", "↻ متابعة تلقائية (1/3)"])

    def test_crash_keeps_partial_work_and_continue_button(self):
        self.app.ctx.cfg.data.setdefault("gui", {})["auto_resume"] = {"enabled": False}
        ev = self.chat(text="write code CRASH_MID", gateway="fake")
        conv_id = self.of(ev, "conversation")[0]["id"]
        meta = self.of(ev, "saved")[0]["meta"]
        self.assertEqual(meta["interrupted"]["reason"], "crash")
        self.assertIn("something broke", meta["interrupted"]["detail"])
        self.assertTrue(any(p.get("name") == "Write" for p in meta["parts"]))   # the step done before the crash
        self.assertEqual(self.ctx.store().conversation(conv_id)["session_id"], "sess-stream-1")  # saved at init
        ev = self.chat(gateway="fake", conversation_id=conv_id, resume=True)
        self.assertFalse(self.of(ev, "error"))
        rec = self.sb.read_record()
        self.assertIn("--resume", rec["argv"])
        self.assertIn("انقطع تنفيذ المهمة", rec["prompt"])
        self.assertIn("خرج Claude Code بخطأ غير متوقع", rec["prompt"])

    def test_closed_mid_reply_is_recovered(self):
        conv_id = self.ctx.store().new_conversation("t", "fake", "", str(self.sb.project))
        self.ctx.store().update_conversation(conv_id, session_id="sess-old", session_gateway="fake")
        self.ctx.store().add_message(conv_id, "user", "make an app", "make an app", {})
        mid = self.ctx.store().add_message(conv_id, "assistant", "half", meta={
            "running": True, "gateway": "fake", "gateway_type": "custom",
            "parts": [{"kind": "tool", "name": "Write", "input": {"file_path": "app.py"}, "result": "ok"}]})
        self.assertEqual(self.app.recover_interrupted(), 1)
        meta = self.ctx.store().messages(conv_id)[-1]["meta"]
        self.assertFalse(meta["running"])
        self.assertEqual(meta["interrupted"]["reason"], "closed")
        self.assertEqual(self.app.recover_interrupted(), 0)
        self.chat(gateway="fake", conversation_id=conv_id, resume=True)
        argv = self.sb.read_record()["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-old")
        self.assertEqual(mid, self.ctx.store().messages(conv_id)[1]["id"])

    def test_lost_session_falls_back_to_handoff(self):
        self._fast_resume()
        first = self.chat(text="first USE_TOOL", gateway="fake")
        conv_id = self.of(first, "conversation")[0]["id"]
        self.ctx.store().update_conversation(conv_id, session_id="gone")
        ev = self.chat(text="next step", gateway="fake", conversation_id=conv_id)
        saved = self.of(ev, "saved")
        self.assertEqual(saved[0]["meta"]["interrupted"]["reason"], "session_missing")
        self.assertNotIn("interrupted", saved[-1]["meta"])
        rec = self.sb.read_record()
        self.assertNotIn("--resume", rec["argv"])
        self.assertIn("ملف تسليم المهمة", rec["prompt"])
        self.assertIn("✔ hello.txt", rec["prompt"])          # files done earlier are listed
        handoff = self.ctx.data_dir / "conversations" / f"handoff-{conv_id}.md"
        self.assertTrue(handoff.is_file())

    def test_stop_cancels_waiting_resume(self):
        saved = dict(gui._REASONS)
        gui._REASONS["rate_limit"] = (saved["rate_limit"][0], True, 120, "continue", 3)
        self.addCleanup(lambda: gui._REASONS.update(saved))
        th, box = self._chat_async(text="FAIL_429", gateway="fake")
        conv_id = None
        for _ in range(300):
            time.sleep(0.1)
            with self.app.lock:
                live = next(iter(self.app.live.items()), None)
            if live and live[1].get("waiting"):
                conv_id = live[0]
                break
        self.assertIsNotNone(conv_id)
        state = json.loads(self.call(f"/api/conversations/{conv_id}/live")[1])
        self.assertTrue(state["running"] and state["waiting"])
        self.assertTrue(json.loads(self.call(f"/api/conversations/{conv_id}")[1])["conversation"]["running"])
        self.assertTrue(json.loads(self.call("/api/stop", {"run_id": "", "conversation_id": conv_id})[1])["ok"])
        th.join(30)
        self.assertFalse(th.is_alive())
        self.assertEqual(len(self.of(box["events"], "saved")), 1)

    def test_report_timing_tells_waiting_from_work(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"FAKE_WAIT": "2.6"}):
            ev = self.chat(text="install TIMED", gateway="fake")
        conv_id = self.of(ev, "conversation")[0]["id"]
        meta = self.of(ev, "saved")[0]["meta"]
        kinds = [e["k"] for e in meta["timeline"]]
        for k in ("gateway_ready", "launch", "init", "model_start", "first_token", "tool_call", "tool_done", "result", "exit"):
            self.assertIn(k, kinds)
        tool = next(p for p in meta["parts"] if p.get("kind") == "tool")
        self.assertGreaterEqual(tool["t_done"] - tool["t_call"], 0.5)
        tm = gui.analyze_timing(meta)
        self.assertGreaterEqual(tm["cats"]["model_wait"], 5.0)       # two ~2.6 s waits for the model
        self.assertGreaterEqual(tm["cats"]["tools"], 0.5)            # the command ran ~0.6 s
        self.assertEqual(len(tm["waits"]), 2)
        self.assertIn("انتظار للبوابة/النموذج", tm["verdict"])
        text = json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"]
        for needle in ("⏱ أُرسل الطلب:", "⏱ أُرسل الأمر:", "اكتمل بالكامل:", "مدة التنفيذ:", "صدرت النتيجة النهائية:",
                       "[نص الرد] ⏱ صدر:", "تحليل الوقت لهذا الرد:", "انتظار النموذج/البوابة حتى أول حرف",
                       "⇐ الحكم:", "ملخص التوقيت", "أطول فترات الانتظار بين الأوامر"):
            self.assertIn(needle, text)

    # ------------------------------------------------------------- verify & fix
    def _verify_cfg(self, **kw):
        import sys
        cfg = {"command": f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'fixed.txt\') else 1)"',
               "max_rounds": 3, "auto_detect": False}
        cfg.update(kw)
        self.app.ctx.cfg.data.setdefault("gui", {})["verify"] = cfg
        fixed = self.sb.project / "fixed.txt"
        self.addCleanup(lambda: fixed.exists() and fixed.unlink())

    def test_verify_fails_then_model_fixes_it(self):
        self._verify_cfg()
        from unittest import mock
        with mock.patch.dict(os.environ, {"FAKE_FIX_AFTER": "1"}):
            ev = self.chat(text="EDIT the code FIX_ON_BLOCK", gateway="fake", verify=True)
        rec = self.sb.read_record()
        reasons = rec["stop_reasons"]
        self.assertEqual(len(reasons), 2)                       # two failed checks sent back, then it passed
        self.assertIn("وفشل التحقق", reasons[0])
        self.assertIn("المحاولة 1 من 3", reasons[0])
        self.assertNotIn("الطريقة الحالية لم تنجح", reasons[0])
        self.assertIn("الطريقة الحالية لم تنجح", reasons[1])     # same error again -> change the approach
        meta = self.of(ev, "saved")[0]["meta"]
        self.assertEqual(meta["verify"]["status"], "passed")
        self.assertEqual([c["code"] for c in meta["verify"]["checks"]], [1, 1, 0])
        self.assertEqual(len(self.of(ev, "verify")), 3)          # the page sees every check live
        argv = rec["argv"]
        sys_prompt = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("طريقة العمل", sys_prompt)
        conv_id = self.of(ev, "conversation")[0]["id"]
        text = json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"]
        self.assertIn("[تحقق آلي] ✔ نجح التحقق", text)
        self.assertIn("الجولة 1: ✘ رمز الخروج 1", text)
        self.assertIn("الجولة 3: ✔", text)

    def test_verify_gives_up_after_max_rounds(self):
        import sys
        self._verify_cfg(command=f'"{sys.executable}" -c "import sys; print(\'Error: boom\'); sys.exit(2)"', max_rounds=2)
        ev = self.chat(text="EDIT it", gateway="fake", verify=True)
        self.assertEqual(len(self.sb.read_record()["stop_reasons"]), 2)
        ver = self.of(ev, "saved")[0]["meta"]["verify"]
        self.assertEqual(ver["status"], "failed")
        self.assertEqual(len(ver["checks"]), 3)
        self.assertIn("Error: boom", ver["checks"][0]["output"])

    def test_verify_without_command_asks_model_to_test_itself(self):
        self._verify_cfg(command="")
        ev = self.chat(text="EDIT something", gateway="fake", verify=True)
        reasons = self.sb.read_record()["stop_reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("تحقق بنفسك", reasons[0])
        self.assertEqual(self.of(ev, "saved")[0]["meta"]["verify"]["status"], "self")
        # a question that changed no files is not checked
        self.chat(text="just a question", gateway="fake", verify=True)
        self.assertEqual(self.sb.read_record()["stop_reasons"], [])

    def test_verify_off_and_project_command(self):
        self._verify_cfg()
        self.chat(text="EDIT x", gateway="fake")
        rec = self.sb.read_record()
        self.assertEqual(rec["stop_reasons"], [])
        self.assertNotIn("ALLINONE_VERIFY", rec["env"])
        pid = json.loads(self.call("/api/projects", {"name": "p", "verify_command": "echo project-check"})[1])["project"]["id"]
        self.chat(text="EDIT y", gateway="fake", project_id=pid, verify=True)
        rec = self.sb.read_record()
        self.assertEqual(rec["env"]["ALLINONE_VERIFY_CMD"], "echo project-check")
        self.assertEqual(rec["stop_reasons"], [])                   # `echo` succeeds

    # ------------------------------------------------------------- visual test (try the program for real)
    def _site(self, with_logo=False):
        site = self.sb.tmp / "site"
        site.mkdir(exist_ok=True)
        (site / "index.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>متجر</title></head><body>'
            '<h1>مرحباً</h1><img src="logo.png"><button onclick="document.body.append(\'تمت\')">أضف</button>'
            '<a href="about.html">عن</a></body></html>', encoding="utf-8")
        (site / "about.html").write_text('<html><head><meta charset="utf-8"><title>عن</title></head><body>عن المتجر</body></html>',
                                         encoding="utf-8")
        pid = json.loads(self.call("/api/projects", {"name": "site", "folder": str(site)})[1])["project"]["id"]
        return site, pid

    def _visual_cfg(self, **kw):
        cfg = {"reviewer": {"gateway": "off"}, "start_timeout": 30, "max_pages": 2}
        cfg.update(kw)
        self.app.ctx.cfg.data.setdefault("gui", {})["visual"] = cfg
        self.app.ctx.cfg.data["gui"]["verify"] = {"max_rounds": 2}

    @unittest.skipUnless(VISUAL_OK, "Playwright and a browser are needed")
    def test_visual_test_runs_the_program_and_sends_problems_back(self):
        self._visual_cfg()
        site, pid = self._site()
        from unittest import mock
        with mock.patch.dict(os.environ, {"FAKE_FIX_FILE": str(site / "logo.png")}):
            ev = self.chat(text="EDIT build the shop page FIX_ON_BLOCK", gateway="fake", project_id=pid, project="", visual=True)
        rec = self.sb.read_record()
        reasons = rec["stop_reasons"]
        self.assertEqual(len(reasons), 1, json.dumps(self.of(ev, "saved")[0]["meta"].get("verify"), ensure_ascii=False)[:1500])
        self.assertIn("جرّب Allinone البرنامج فعلياً", reasons[0])
        self.assertIn("logo.png", reasons[0])                       # the missing image was found
        self.assertIn("01-start", reasons[0])                      # screenshots are listed for the model
        ver = self.of(ev, "saved")[0]["meta"]["verify"]
        self.assertEqual(ver["visual_status"], "passed")
        first, second = ver["visual"]
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        names = [s["name"] for s in first["shots"]]
        self.assertIn("start", names)
        self.assertIn("mobile", names)
        self.assertTrue(any(n.startswith("page-") for n in names))  # visited a link by itself
        for shot in first["shots"]:
            self.assertTrue(Path(shot["path"]).is_file())
            self.assertTrue(shot["path"].endswith(".jpg"))
        start = next(s for s in first["shots"] if s["name"] == "start")
        self.assertLess(start["tokens"], 900)                        # 960×600 ≈ 770 tokens, not 1280×800+
        self.assertEqual(len(self.of(ev, "visual")), 2)               # the page sees each try live
        self.assertIn("allinone_browser", rec["mcp"]["mcpServers"])    # browser tools for the model
        self.assertEqual(rec["mcp"]["mcpServers"]["allinone_browser"]["env"]["ALLINONE_BROWSER_IMAGES"], "0")
        argv = rec["argv"]
        sys_prompt = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("جرّب البرنامج فعلياً", sys_prompt)
        # screenshots are served to the page, nothing else is
        url = f"/api/image?path={urllib.request.quote(first['shots'][0]['path'])}&t={self.app.token}"
        self.assertEqual(self.call(url)[0], 200)
        self.assertEqual(self.call(f"/api/image?path={urllib.request.quote(str(ROOT / 'README.md'))}")[0], 403)
        conv_id = self.of(ev, "conversation")[0]["id"]
        report = json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"]
        self.assertIn("[تجربة البرنامج فعلياً] الجولة 1: ✘", report)
        self.assertIn("📷 start:", report)

    @unittest.skipUnless(VISUAL_OK, "Playwright and a browser are needed")
    def test_visual_second_opinion_from_a_model_that_sees(self):
        self._visual_cfg(reviewer={"gateway": "fake", "model": ""})
        site, pid = self._site()
        (site / "logo.png").write_bytes(b"")
        ev = self.chat(text="EDIT the page REVIEW_FAIL", gateway="fake", project_id=pid, project="", visual=True)
        rec = self.sb.read_record()
        reasons = rec["stop_reasons"]
        self.assertTrue(reasons)
        self.assertIn("مراجعة الصور", reasons[0])
        self.assertIn("زر «أضف» مخفي", reasons[0])
        review = self.of(ev, "visual_review")[0]
        self.assertEqual(review["verdict"], "problems")
        review_prompt = Path(os.environ.get("FAKE_CLAUDE_RECORD") or self.sb.env["FAKE_CLAUDE_RECORD"]).with_name(
            Path(self.sb.env["FAKE_CLAUDE_RECORD"]).name + ".review.txt").read_text(encoding="utf-8")
        self.assertIn("REVIEW_FAIL", review_prompt)                   # the reviewer knows what was asked
        self.assertIn("01-start.jpg", review_prompt)
        self.assertEqual(self.of(ev, "saved")[0]["meta"]["verify"]["visual_status"], "failed")

    def test_visual_not_installed_is_explained(self):
        from unittest import mock
        with mock.patch.object(gui.GuiApp, "visual_python", return_value=None):
            ev = self.chat(text="EDIT x", gateway="fake", visual=True)
        self.assertTrue(any("allinone install visual" in n["text"] for n in self.of(ev, "notice")))
        self.assertNotIn("allinone_browser", self.sb.read_record()["mcp"]["mcpServers"])

    # ------------------------------------------------------------- speed: context, failover, hand-over
    def test_context_limits_for_gateway_models(self):
        self.chat(text="hi", gateway="fake")
        env = self.sb.read_record()["env"]
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "128000")    # custom gateways
        self.assertEqual(env["BASH_MAX_OUTPUT_LENGTH"], "12000")

    def test_failover_to_next_gateway(self):
        os.environ["FAKE_AUTH"] = "1"
        self.addCleanup(lambda: os.environ.pop("FAKE_AUTH", None))
        self.app.ctx.cfg.data.setdefault("gui", {})["failover"] = {"order": ["fake", "claude"]}
        ev = self.chat(text="build it FAIL_429", gateway="fake", failover=True)
        notes = " ".join(n["text"] for n in self.of(ev, "notice"))
        self.assertIn("↪", notes)
        start = self.of(ev, "resume_start")[0]
        self.assertEqual(start["gateway"], "claude")
        conv_id = self.of(ev, "conversation")[0]["id"]
        moved = [m for m in self.ctx.store().messages(conv_id) if (m.get("meta") or {}).get("failover") == "claude"][0]
        self.assertIn("ملف تسليم المهمة", moved["sent"])                # it carries the work so far
        saved = self.of(ev, "saved")
        self.assertEqual(saved[1]["meta"]["gateway"], "claude")
        self.assertEqual(saved[-1]["meta"]["gateway"], "claude")
        self.assertNotIn("interrupted", saved[-1]["meta"])
        # without the switch it waits on the same gateway instead
        self.app.ctx.cfg.data["gui"]["auto_resume"] = {"enabled": False}
        ev = self.chat(text="again FAIL_429", gateway="fake")
        self.assertEqual(self.of(ev, "resume_start"), [])

    def test_new_chat_continues_from_long_one(self):
        old = self.of(self.chat(text="the original long task USE_TOOL", gateway="fake"), "conversation")[0]["id"]
        ev = self.chat(text="carry on", gateway="fake", handoff_from=old)
        new = self.of(ev, "conversation")[0]["id"]
        self.assertNotEqual(new, old)
        rec = self.sb.read_record()
        self.assertIn("ملف تسليم المهمة", rec["prompt"])
        self.assertIn("the original long task", rec["prompt"])
        self.assertTrue(rec["prompt"].rstrip().endswith("carry on"))
        self.assertGreater(self.of(ev, "saved")[0]["meta"]["context_tokens"], 0)

    def test_fast_model_picks_and_rests_models(self):
        class FakeAdmin:
            calls = []
            def tool_models(self, min_context=0, exclude=None):
                FakeAdmin.calls.append(set(exclude or ()))
                return [m for m in ({"modelId": "llama-fast", "platform": "groq", "contextWindow": 131072},
                                    {"modelId": "gemini-flash", "platform": "google", "contextWindow": 1000000})
                        if m["modelId"] not in (exclude or ())]
        from unittest import mock
        gw = mock.Mock(type_name="freellmapi")
        with mock.patch.object(gui.GuiApp, "keys_admin", return_value=FakeAdmin()):
            ev = []
            self.assertEqual(self.app.pick_fast_model(gw, ev.append), ("llama-fast", 131072))
            self.app.model_cooldown["llama-fast"] = time.time() + 300
            self.assertEqual(self.app.pick_fast_model(gw, ev.append), ("gemini-flash", 1000000))
        self.assertIn("llama-fast", FakeAdmin.calls[-1])
        env = self.app.context_env(mock.Mock(type_name="freellmapi", name="freellmapi"), 131072)
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "64000")       # the smaller of config and model

    def test_short_report_and_gateway_diagnosis(self):
        self.app.ctx.cfg.data.setdefault("gui", {})["auto_resume"] = {"enabled": False}
        ev = self.chat(text="please USE_TOOL now", gateway="fake")
        conv_id = self.of(ev, "conversation")[0]["id"]
        self.chat(text="FAIL_429", gateway="fake", conversation_id=conv_id)
        full = json.loads(self.call(f"/api/conversations/{conv_id}/report", {})[1])["text"]
        short = json.loads(self.call(f"/api/conversations/{conv_id}/report", {"compact": True})[1])
        self.assertIn("-short", short["name"])
        self.assertIn("المدخلات:", full)
        self.assertNotIn("المدخلات:", short["text"])                      # successful steps are one line
        self.assertIn("[الخطوة 1] ✔ Write: hello.txt", short["text"])
        lines = gui.gateway_diagnosis([{"role": "assistant", "meta": {"parts": [
            {"kind": "text", "text": "API Error: Request rejected (429) · All models exhausted: 48 routes checked "
             "(6 rate-limited or on cooldown, 6 prompt too large for the model, 36 model lacks tool-calling). "
             "Soonest reset ~5m. 380 models skipped: no key configured for their platform."},
            {"kind": "tool", "name": "Bash", "input": {"command": "python -m http.server 8000"},
             "result": "Command did not complete within its 120s timeout and was moved to the background"}]}}])
        text = "\n".join(lines)
        for needle in ("48 مساراً", "لا تدعم الأدوات", "380 نموذجاً", "5m", "python -m http.server 8000", "app_start"):
            self.assertIn(needle, text)

    def test_app_tools_start_once_in_background(self):
        import subprocess as sp
        import sys as _sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.html").write_text("<h1>ok</h1>")
            cmd = f'"{_sys.executable}" -m http.server 0 --bind 127.0.0.1'
            calls = [("app_start", {"command": cmd, "cwd": tmp, "wait_seconds": 20}),
                     ("app_start", {"command": cmd, "cwd": tmp, "wait_seconds": 20}),
                     ("app_logs", {}), ("app_stop", {})]
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                     "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]
            msgs += [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                     for i, (n, a) in enumerate(calls, 2)]
            out = sp.run([_sys.executable, str(ROOT / "claude" / "mcp" / "allinone_tools.py")],
                         input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True,
                         env=dict(os.environ, ALLINONE_DATA_DIR=str(Path(tmp) / "d")), timeout=90).stdout.decode()
            replies = {r["id"]: r["result"]["content"][0]["text"] for r in map(json.loads, out.splitlines())
                       if (r.get("id") or 0) > 1}
            self.assertIn("http://", replies[2])                         # started and found its URL
            self.assertIn("يعمل مسبقاً", replies[3])                      # not started twice
            self.assertIn("أُوقف", replies[5])

    def test_wait_follows_the_gateway_reset_time(self):
        d = gui.diagnose("freellmapi", "API Error: Request rejected (429) · All models exhausted … Soonest reset ~5m.")
        self.assertEqual((d["reason"], d["wait"]), ("rate_limit", 305))
        self.assertEqual(gui.diagnose("freellmapi", "429 All models rate-limited. Soonest cooldown reset ~17s")["wait"], 22)
        self.assertEqual(gui.diagnose("omniroute", "429 Too Many Requests")["wait"], 60)   # nothing said: default


    def test_quick_question_goes_straight_to_the_gateway(self):
        ev = self.chat(text=QUESTION, gateway="fake", quick=True)
        conv_id = self.of(ev, "conversation")[0]["id"]
        self.assertFalse(self.sb.record.exists())                  # smarter Code never ran
        text = "".join(e["text"] for e in self.of(ev, "delta"))
        self.assertEqual(text, "echo: " + QUESTION)                # streamed piece by piece
        self.assertGreater(len(self.of(ev, "delta")), 1)
        msgs = self.ctx.store().messages(conv_id)
        self.assertTrue(msgs[0]["meta"]["quick"])
        self.assertTrue(msgs[1]["meta"]["quick"])
        self.assertEqual(msgs[1]["display"], "echo: " + QUESTION)
        self.assertEqual(msgs[1]["meta"]["context_tokens"], 7)
        req = [r for r in self.sb.gateway_requests() if (r.get("body") or {}).get("messages")][-1]["body"]
        self.assertTrue(req["stream"])
        self.assertNotIn("tools", req)
        self.assertIn("سؤال سريع", req["system"])
        # the next quick question carries the conversation
        ev2 = self.chat(text="وماذا عن البصرة؟", gateway="fake", quick=True, conversation_id=conv_id)
        req = [r for r in self.sb.gateway_requests() if (r.get("body") or {}).get("messages")][-1]["body"]
        self.assertEqual([m["role"] for m in req["messages"]], ["user", "assistant", "user"])
        self.assertEqual(req["messages"][1]["content"], "echo: " + QUESTION)
        self.assertIn("echo: وماذا عن البصرة؟", "".join(e["text"] for e in self.of(ev2, "delta")))
        # an ordinary turn afterwards uses smarter Code again
        self.chat(text="اكمل", gateway="fake", conversation_id=conv_id)
        self.assertTrue(self.sb.record.exists())

    def test_quick_question_gateway_error_is_reported(self):
        ev = self.chat(text="FAIL_429 " + QUESTION, gateway="fake", quick=True)
        err = self.of(ev, "error")
        self.assertTrue(err and "429" in err[0]["text"], ev)
        saved = self.of(ev, "saved")[0]["meta"]
        self.assertTrue(saved["is_error"] and saved["quick"])
        self.assertTrue(saved.get("interrupted"))

    def test_light_mode_sends_small_requests(self):
        self.chat(text=QUESTION, gateway="fake", light=True)
        rec = self.sb.read_record()
        argv = rec["argv"]
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit,Write,Glob,Grep")
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(rec["env"].get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"), "1")
        self.assertEqual(rec["env"].get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), "8192")
        # without it: everything as before
        self.chat(text=QUESTION, gateway="fake")
        argv = self.sb.read_record()["argv"]
        self.assertNotIn("--tools", argv)

    def test_quick_question_on_claude_runs_without_tools(self):
        os.environ["FAKE_AUTH"] = "1"
        ev = self.chat(text="hi", gateway="claude", quick=True)
        self.assertTrue(self.of(ev, "delta"))
        argv = self.sb.read_record()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)


class FreeLLMAPIAdminTests(unittest.TestCase):
    """The keys page talks to FreeLLMAPI's dashboard API; a small stand-in answers here."""

    def setUp(self):
        import http.server
        import tempfile
        import threading
        state = self.state = {"users": 0, "keys": [], "keyless_enabled": False, "logins": 0}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return self.headers.get("Authorization") == "Bearer tok"

            def do_GET(self):
                if self.path == "/api/auth/status":
                    return self._send(200, {"needsSetup": state["users"] == 0})
                if not self._authed():
                    return self._send(401, {"error": {"message": "Authentication required"}})
                if self.path == "/api/keys/providers":
                    have = {k["platform"] for k in state["keys"]}
                    return self._send(200, {"providers": [
                        {"platform": "groq", "name": "Groq", "kind": "chat", "keyless": False,
                         "configured": "groq" in have, "enabledKeyCount": int("groq" in have)},
                        {"platform": "google", "name": "Google", "kind": "chat", "keyless": False,
                         "configured": False, "enabledKeyCount": 0},
                        {"platform": "kilo", "name": "Kilo", "kind": "chat", "keyless": True,
                         "configured": state["keyless_enabled"], "enabledKeyCount": int(state["keyless_enabled"])},
                        {"platform": "brave", "name": "Brave", "kind": "search", "keyless": False,
                         "configured": False, "enabledKeyCount": 0}]})
                if self.path == "/api/models":
                    return self._send(200, [
                        {"platform": "groq", "modelId": "llama-70b", "supportsTools": True, "enabled": True, "speedRank": 2, "contextWindow": 131072},
                        {"platform": "groq", "modelId": "llama-8b", "supportsTools": True, "enabled": True, "speedRank": 1, "contextWindow": 8192},
                        {"platform": "google", "modelId": "gemini", "supportsTools": True, "enabled": True, "speedRank": 1, "contextWindow": 1000000},
                        {"platform": "google", "modelId": "gemma", "supportsTools": False, "enabled": True, "speedRank": 1, "contextWindow": 8192},
                        {"platform": "kilo", "modelId": "kilo-auto", "supportsTools": True, "enabled": True, "speedRank": 5, "contextWindow": 64000}])
                if self.path == "/api/keys/keyless-available":
                    return self._send(200, {"providers": [] if state["keyless_enabled"] else [{"platform": "kilo", "name": "Kilo"}]})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/auth/setup":
                    state["users"] = 1
                    state["creds"] = (body["email"], body["password"])
                    return self._send(201, {"token": "tok"})
                if self.path == "/api/auth/login":
                    state["logins"] += 1
                    ok = state.get("creds") == (body.get("email"), body.get("password"))
                    return self._send(200, {"token": "tok"}) if ok else self._send(401, {"error": {"message": "Invalid"}})
                if not self._authed():
                    return self._send(401, {})
                if self.path == "/api/keys":
                    state["keys"].append(body)
                    return self._send(200, {"modelsAvailable": 2, "status": "unknown"})
                if self.path == "/api/keys/keyless-available/enable":
                    state["keyless_enabled"] = True
                    return self._send(200, {"enabled": ["kilo"]})
                self._send(404, {})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        from controller.config import Secrets
        from controller.freellmapi_admin import FreeLLMAPIAdmin
        FreeLLMAPIAdmin._token_cache.clear()
        self.ctx = type("C", (), {"secrets": Secrets(Path(self.tmp.name) / "secrets.json")})()
        self.gw = type("G", (), {"name": "freellmapi", "base_url": f"http://127.0.0.1:{self.srv.server_address[1]}",
                                 "install_dir": self.tmp.name})()
        self.Admin = FreeLLMAPIAdmin

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_keys_page_flow(self):
        admin = self.Admin(self.ctx, self.gw)
        st = admin.status()                                          # a fresh dashboard is claimed for Allinone
        self.assertEqual(self.ctx.secrets.get("freellmapi", "dashboard_email"), "allinone@localhost.local")
        self.assertEqual(st["usable_tool_models"], 0)
        self.assertEqual(st["keyless_available"], ["Kilo"])
        names = [p["platform"] for p in st["providers"]]
        self.assertNotIn("brave", names)                             # search keys are not for chat
        self.assertEqual(names[:2], ["groq", "google"])              # most tool-capable models first
        self.assertEqual(st["providers"][0]["tool_models"], 2)
        self.assertTrue(st["providers"][0]["url"].startswith("https://console.groq.com"))
        admin.add_key("groq", "  gsk_abc  ")
        self.assertEqual(self.state["keys"], [{"platform": "groq", "key": "gsk_abc"}])
        admin.enable_keyless()
        st = admin.status()
        self.assertEqual(st["usable_tool_models"], 3)                # groq ×2 + kilo
        fast = admin.tool_models(min_context=32000)
        self.assertEqual([m["modelId"] for m in fast], ["llama-70b", "kilo-auto"])   # 8k context excluded
        # a new Allinone process signs in with the saved password
        self.Admin._token_cache.clear()
        self.Admin(self.ctx, self.gw).status()
        self.assertEqual(self.state["logins"], 1)

    def test_existing_dashboard_needs_password_once(self):
        from controller.freellmapi_admin import NeedCredentials
        self.state["users"] = 1
        self.state["creds"] = ("me@x.com", "secret123")
        with self.assertRaises(NeedCredentials):
            self.Admin(self.ctx, self.gw).status()
        self.Admin(self.ctx, self.gw).save_credentials("me@x.com", "secret123")
        self.assertIn("providers", self.Admin(self.ctx, self.gw).status())


class VerifyHookTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_stop", ROOT / "claude" / "hooks" / "verify_stop.py")
        self.v = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.v)

    def test_detects_python_tests(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self.v.detect_command(Path(tmp)))
            (Path(tmp) / "tests").mkdir()
            cmd = self.v.detect_command(Path(tmp))
            self.assertTrue("pytest" in cmd or "unittest" in cmd, cmd)

    def test_same_error_is_recognised(self):
        a = self.v.signature("Traceback...\nFile /a/b.py, line 12\nValueError: bad value 3")
        b = self.v.signature("Traceback...\nFile /c/d.py, line 40\nValueError: bad value 7")
        c = self.v.signature("Traceback...\nKeyError: 'name'")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class BrandingTests(unittest.TestCase):
    def tearDown(self):
        from controller import i18n
        i18n.set_brand("")

    def test_display_name_hides_the_real_one_but_not_commands(self):
        from controller import i18n
        i18n.set_brand("smarter")
        self.assertEqual(i18n.tr("Claude Code is not installed. Run: allinone install claude",
                                 "Claude Code غير مثبت. نفّذ: allinone install claude"),
                         "smarter Code غير مثبت. نفّذ: allinone install claude" if i18n.language() == "ar"
                         else "smarter Code is not installed. Run: allinone install claude")
        self.assertEqual(i18n.tr("{p}", "{p}", p="/home/me/Claude/x"), "/home/me/Claude/x")   # values untouched
        self.assertEqual(i18n.mask_models("anthropic/claude-sonnet-4-5 · claude-opus-4"), "smarter/smarter-sonnet-4-5 · smarter-opus-4")
        self.assertNotIn("Anthropic", i18n.brand("حساب Anthropic"))
        i18n.set_brand("")
        self.assertEqual(i18n.brand("Claude"), "Claude")                                        # empty = real name


class VisualPieces(unittest.TestCase):
    def test_parse_review(self):
        self.assertEqual(gui.parse_review("looks fine\nVERDICT: OK"), ("ok", []))
        v, probs = gui.parse_review("VERDICT: PROBLEMS\n- a.png: broken\n- b.png: cut text")
        self.assertEqual((v, probs), ("problems", ["a.png: broken", "b.png: cut text"]))
        self.assertEqual(gui.parse_review("no idea")[0], "unclear")

    def test_detect_app(self):
        import sys as _sys
        import tempfile
        _sys.path.insert(0, str(ROOT / "claude" / "visual"))
        import browser_lib
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.assertIsNone(browser_lib.detect_app(t))
            (t / "index.html").write_text("<h1>x</h1>")
            self.assertIn("http.server", browser_lib.detect_app(t)["command"])
            (t / "package.json").write_text('{"scripts": {"dev": "vite"}}')
            self.assertEqual(browser_lib.detect_app(t)["command"], "npm run dev")
            (t / "package.json").unlink()
            (t / "index.html").unlink()
            (t / "app.py").write_text("import tkinter\n")
            self.assertEqual(browser_lib.detect_app(t)["kind"], "desktop")

    @unittest.skipUnless(VISUAL_OK, "Playwright and a browser are needed")
    def test_browser_tools_for_the_model(self):
        import subprocess as sp
        import sys as _sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site"
            site.mkdir()
            (site / "index.html").write_text('<html><head><meta charset="utf-8"><title>T</title></head><body>'
                                             '<input id="q" placeholder="ابحث"><button onclick="document.getElementById(\'r\').textContent='
                                             'document.getElementById(\'q\').value">بحث</button><p id="r"></p>'
                                             '<script>console.error("boom")</script></body></html>', encoding="utf-8")
            server = sp.Popen([_sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1", "--directory", str(site)],
                              stdout=sp.PIPE, stderr=sp.STDOUT)
            try:
                line = server.stdout.readline().decode()
                port = int(line.split("port ")[1].split()[0])
                calls = [("browser_open", {"url": f"http://127.0.0.1:{port}/"}),
                         ("browser_type", {"target": "#q", "text": "قهوة"}),
                         ("browser_click", {"target": "بحث"}),
                         ("browser_screenshot", {"name": "after search"}),
                         ("browser_console", {}),
                         ("browser_close", {})]
                msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                         "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}]
                msgs += [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                         for i, (n, a) in enumerate(calls, 2)]
                env = dict(os.environ, ALLINONE_VISUAL_OUT=str(Path(tmp) / "shots"), ALLINONE_BROWSER_IMAGES="1",
                           ALLINONE_BROWSER=VISUAL_BROWSER)
                out = sp.run([_sys.executable, str(ROOT / "claude" / "mcp" / "browser_server.py")],
                             input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True, env=env,
                             timeout=120).stdout.decode()
            finally:
                server.kill()
            replies = {r["id"]: r for r in map(json.loads, out.splitlines()) if r.get("id")}
            text = lambda i: replies[i]["result"]["content"][0]["text"]  # noqa: E731
            self.assertIn("ابحث", text(2) + text(3))                   # the page as text, with its fields
            self.assertIn("#q", text(2))
            self.assertIn("قهوة", text(4))                              # clicking showed the result
            shot = replies[5]["result"]["content"]
            self.assertEqual(shot[1]["type"], "image")                   # models that see get the picture
            self.assertTrue(list((Path(tmp) / "shots").glob("*.jpg")))
            self.assertIn("boom", text(6))                              # console errors are reported


class TimingAnalysisTests(unittest.TestCase):
    def meta(self, tl, **kw):
        return {"started_at": 1000.0, "ended_at": 1000.0 + tl[-1]["t"], "timeline": tl, "gateway_type": "omniroute",
                "parts": [{"kind": "tool", "id": "a", "name": "Bash"}], **kw}

    def test_slow_gateway_is_waiting_not_work(self):
        tm = gui.analyze_timing(self.meta([
            {"t": 0.5, "k": "gateway_ready"}, {"t": 0.6, "k": "launch"}, {"t": 2, "k": "init"},
            {"t": 92, "k": "model_start"}, {"t": 95, "k": "first_token"},
            {"t": 97, "k": "tool_call", "id": "a"}, {"t": 97.1, "k": "model_end", "chars": 40, "max_gap": 0.3},
            {"t": 99, "k": "tool_done", "id": "a"}, {"t": 160, "k": "first_token"},
            {"t": 165, "k": "model_end", "chars": 500, "max_gap": 0.2}, {"t": 165.5, "k": "result"},
            {"t": 166, "k": "exit"}]))
        self.assertAlmostEqual(tm["cats"]["model_wait"], 93 + 61, delta=0.5)
        self.assertGreater(tm["pct"]["wait"], 80)
        self.assertIn("OmniRoute", tm["verdict"])
        self.assertEqual(sum("انتظر النموذج" in f["text"] for f in tm["flags"]), 2)

    def test_long_command_is_work_and_approval_is_yours(self):
        tm = gui.analyze_timing(self.meta([
            {"t": 0.1, "k": "gateway_ready"}, {"t": 0.2, "k": "launch"}, {"t": 1, "k": "init"},
            {"t": 2, "k": "model_start"}, {"t": 3, "k": "first_token"}, {"t": 4, "k": "tool_call", "id": "a"},
            {"t": 4.1, "k": "model_end", "chars": 30, "max_gap": 0.1}, {"t": 304, "k": "tool_done", "id": "a"},
            {"t": 306, "k": "first_token"}, {"t": 308, "k": "model_end", "chars": 100, "max_gap": 0.1},
            {"t": 308.2, "k": "result"}], approvals=[{"t_ask": 1004.5, "t_done": 1034.5, "auto": False}]))
        self.assertAlmostEqual(tm["cats"]["approval"], 30, delta=0.5)
        self.assertAlmostEqual(tm["cats"]["tools"], 270, delta=0.5)
        self.assertIn("تنفيذ أوامر فعلية", tm["verdict"])
        self.assertTrue(any("وقت عمل فعلي" in f["text"] for f in tm["flags"]))
        self.assertAlmostEqual(tm["tools"]["a"]["approval"], 30, delta=0.5)

    def test_stall_in_the_middle_of_an_answer(self):
        tm = gui.analyze_timing(self.meta([
            {"t": 1, "k": "init"}, {"t": 2, "k": "first_token"},
            {"t": 80, "k": "model_end", "chars": 300, "max_gap": 70}, {"t": 80.5, "k": "result"}]))
        texts = " ".join(f["text"] for f in tm["flags"])
        self.assertIn("توقف البث", texts)
        self.assertIn("كتابة بطيئة", texts)

    def test_old_messages_without_timing(self):
        self.assertIsNone(gui.analyze_timing({"parts": []}))


class DiagnoseTests(unittest.TestCase):
    def test_reasons_per_gateway(self):
        d = gui.diagnose("omniroute", "API Error: 429 Too Many Requests")
        self.assertEqual((d["reason"], d["retry"]), ("rate_limit", True))
        self.assertIn("OmniRoute", d["advice"])
        self.assertIn("FreeLLMAPI", gui.diagnose("freellmapi", "quota exhausted")["advice"])
        self.assertEqual(gui.diagnose("freellmapi", "fetch failed", gw_up=False)["reason"], "gateway_down")
        self.assertEqual(gui.diagnose("omniroute", "connect ECONNREFUSED 127.0.0.1:20128")["reason"], "network")
        self.assertEqual(gui.diagnose("anthropic", "Invalid API key · Please run /login")["reason"], "auth")
        self.assertFalse(gui.diagnose("anthropic", "Please run /login")["retry"])
        self.assertEqual(gui.diagnose("custom", "prompt is too long: 250000 tokens")["mode"], "handoff")
        self.assertEqual(gui.diagnose("custom", "", subtype="error_max_turns")["reason"], "max_turns")
        self.assertEqual(gui.diagnose("custom", "x", stopped=True)["retry"], False)

    def test_claude_usage_limit_reset_time(self):
        now = time.mktime((2026, 11, 25, 13, 10, 0, 0, 0, -1))
        d = gui.diagnose("anthropic", "Claude AI usage limit reached|%d" % (now + 3600), now=now)
        self.assertEqual(d["reason"], "usage_limit")
        self.assertEqual(d["resume_at"], now + 3600)
        self.assertEqual(d["wait"], 3630)
        d = gui.diagnose("anthropic", "5-hour limit reached ∙ resets 3pm", now=now)
        self.assertEqual(time.localtime(d["resume_at"]).tm_hour, 15)
        d = gui.diagnose("anthropic", "You've hit your limit · resets 11am", now=now)
        self.assertEqual(d["resume_at"] - now, 22 * 3600 - 10 * 60)    # tomorrow
        d = gui.diagnose("anthropic", "usage limit reached")
        self.assertFalse(d["retry"])
        self.assertIn("OmniRoute", d["advice"])


class StoreMigrationTests(unittest.TestCase):
    def test_old_database_gets_project_column(self):
        import sqlite3
        import tempfile
        from controller.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, "
                         "updated REAL NOT NULL, title TEXT NOT NULL, gateway TEXT NOT NULL DEFAULT '', model TEXT NOT "
                         "NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', session_id TEXT, session_gateway TEXT)")
            conn.execute("INSERT INTO conversations(created, updated, title) VALUES (1, 1, 'old chat')")
            conn.commit()
            conn.close()
            st = Store(db)
            self.assertEqual(st.conversations()[0]["title"], "old chat")
            self.assertIsNone(st.conversations()[0]["project_id"])
            pid = st.new_project(name="p")
            cid = st.new_conversation("new", "fake", "", tmp, project_id=pid)
            self.assertEqual([r["id"] for r in st.conversations(project_id=pid)], [cid])


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
