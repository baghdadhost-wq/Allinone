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
        self.assertNotIn("--append-system-prompt-file", self.sb.read_record()["argv"])
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
        self.assertIn("01-start.png", review_prompt)
        self.assertEqual(self.of(ev, "saved")[0]["meta"]["verify"]["visual_status"], "failed")

    def test_visual_not_installed_is_explained(self):
        from unittest import mock
        with mock.patch.object(gui.GuiApp, "visual_python", return_value=None):
            ev = self.chat(text="EDIT x", gateway="fake", visual=True)
        self.assertTrue(any("allinone install visual" in n["text"] for n in self.of(ev, "notice")))
        self.assertNotIn("allinone_browser", self.sb.read_record()["mcp"]["mcpServers"])


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
            self.assertTrue(list((Path(tmp) / "shots").glob("*.png")))
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
