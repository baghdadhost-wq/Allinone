#!/usr/bin/env python3
"""Allinone MCP server: tools Claude Code can call in every session.

Built in:
  gateway_info     which gateway / model this session runs through
  gateway_models   the models the gateway can route to
  ask_model        send one prompt to another model through the same gateway
  memory_save      remember a note across sessions and gateways
  memory_search    find saved notes
  memory_recent    list the latest notes
  memory_forget    delete a note
  task_list        recent tasks run by the Allinone controller
  task_show        one task's prompt and result
  app_start        start the program (a server or app that keeps running) in the background, get its URL
  app_logs         the latest output of a program started with app_start
  app_stop         stop it (programs started here also stop when the session ends)

Your own tools: drop a .py file into claude/mcp/tools/ (see tools/README.md).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ALLINONE_ROOT") or HERE.parent.parent)
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from mcp_kit import log, serve, tool  # noqa: E402

try:
    from controller.store import Store  # noqa: E402
except Exception:  # pragma: no cover - the controller package should always be next to us
    Store = None

GATEWAY = os.environ.get("ALLINONE_GATEWAY", "")
GATEWAY_TITLE = os.environ.get("ALLINONE_GATEWAY_TITLE", GATEWAY)
GATEWAY_URL = (os.environ.get("ALLINONE_GATEWAY_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
TOKEN = os.environ.get("ALLINONE_GATEWAY_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
MODEL = os.environ.get("ALLINONE_MODEL") or os.environ.get("ANTHROPIC_MODEL") or ""
DB_PATH = Path(os.environ.get("ALLINONE_DB") or ROOT / "data" / "allinone.db")

_store = None


def store():
    global _store
    if _store is None:
        if Store is None:
            raise RuntimeError("controller.store is not importable")
        _store = Store(DB_PATH)
    return _store


def _http(method: str, path: str, body=None, timeout: float = 120.0):
    if not GATEWAY_URL:
        raise RuntimeError("No gateway URL in this session (ALLINONE_GATEWAY_URL is empty).")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(GATEWAY_URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("x-api-key", TOKEN)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway answered HTTP {exc.code}: {text[:500]}")


# ------------------------------------------------------------------ gateway
@tool
def gateway_info() -> dict:
    """Show which gateway (OmniRoute, FreeLLMAPI, ...) and model this Claude Code session is using."""
    return {"gateway": GATEWAY_TITLE or GATEWAY, "url": GATEWAY_URL, "model": MODEL or "(gateway default)",
            "memory_db": str(DB_PATH)}


@tool
def gateway_models(filter: str = "") -> str:
    """List model ids the active gateway can route to. Optional `filter` keeps ids containing that text."""
    _, data = _http("GET", "/v1/models", timeout=20)
    items = (data or {}).get("data") or (data or {}).get("models") or []
    ids = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
    if filter:
        ids = [i for i in ids if filter.lower() in i.lower()]
    return "\n".join(ids) if ids else "(no models returned)"


@tool
def ask_model(prompt: str, model: str = "", system: str = "", max_tokens: int = 2048) -> str:
    """Send a single prompt to a (possibly different) model through the same gateway and return its answer.
    Use it to get a second opinion, translate, or hand a self-contained sub-question to a cheaper/faster model.
    Leave `model` empty to use the session's model."""
    body = {"model": model or MODEL or "auto", "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    _, data = _http("POST", "/v1/messages", body=body, timeout=300)
    parts = [b.get("text", "") for b in (data or {}).get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    answer = "\n".join(p for p in parts if p).strip()
    served = (data or {}).get("model") or body["model"]
    return f"[{served}]\n{answer}" if answer else json.dumps(data, ensure_ascii=False)[:2000]


# ------------------------------------------------------------------- memory
@tool
def memory_save(text: str, tags: str = "") -> str:
    """Save a note to Allinone's long-term memory (shared by every session and gateway).
    Use for decisions, preferences, facts about the project the user wants remembered."""
    mid = store().remember(text, tags=tags, source=f"claude:{GATEWAY}")
    return f"Saved memory #{mid}"


@tool
def memory_search(query: str, limit: int = 10) -> str:
    """Search saved notes (all words must match, in the text or the tags)."""
    rows = store().search_memory(query, int(limit))
    return _fmt_memory(rows) or "No matching notes."


@tool
def memory_recent(limit: int = 10) -> str:
    """List the most recently saved notes."""
    return _fmt_memory(store().recent_memory(int(limit))) or "Memory is empty."


@tool
def memory_forget(id: int) -> str:
    """Delete a saved note by its number."""
    return "Deleted." if store().forget(int(id)) else f"No note #{id}."


def _fmt_memory(rows) -> str:
    out = []
    for r in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(r["created"]))
        tags = f" [{r['tags']}]" if r["tags"] else ""
        out.append(f"#{r['id']} ({when}){tags}: {r['text']}")
    return "\n".join(out)


# -------------------------------------------------------------------- tasks
@tool
def task_list(limit: int = 10) -> str:
    """Recent tasks that the Allinone controller ran through Claude Code (id, status, gateway, prompt)."""
    rows = store().list_tasks(int(limit))
    return "\n".join(f"#{r['id']} [{r['status']}] {r['gateway']}: {(r['prompt'] or '')[:100]}" for r in rows) \
        or "No tasks yet."


@tool
def task_show(id: int) -> str:
    """Show one controller task: its prompt and its result."""
    r = store().get_task(int(id))
    if not r:
        return f"No task #{id}."
    return f"Task #{r['id']} [{r['status']}] via {r['gateway']}\nPrompt:\n{r['prompt']}\n\nResult:\n{r['result'] or ''}"


# --------------------------------------------------------------- user tools
# ------------------------------------------------------------------ programs that keep running
# A server started in the foreground never finishes: the Bash tool then waits for its whole timeout
# (2 minutes) and the model often starts it again. These tools start it once, in the background,
# wait until it answers and return its URL.
sys.path.insert(0, str(ROOT / "claude" / "visual"))
_apps: dict = {}


def _stop_all() -> None:
    for app in list(_apps.values()):
        try:
            app.stop()
        except Exception:
            pass


import atexit  # noqa: E402

atexit.register(_stop_all)


@tool
def app_start(command: str = "", cwd: str = "", url: str = "", wait_seconds: int = 60) -> str:
    """Start the program (dev server, web app, API, desktop app) in the BACKGROUND and wait until it answers.
    Use this instead of running a server with Bash. Empty `command` = detect (npm run dev/start, Django,
    Flask/FastAPI/Streamlit, or a static index.html). Returns the URL and the first output lines.
    If the same command is already running it is reused, not started twice."""
    import browser_lib as bl  # stdlib only for starting programs
    folder = Path(cwd or os.getcwd())
    kind = "web"
    if not command:
        found = bl.detect_app(folder, sys.executable)
        if not found:
            return "لم أعرف كيف يُشغَّل هذا المشروع؛ مرّر command صراحة (مثل: npm run dev)."
        command, url, kind = found["command"], url or found["url"], found["kind"]
    key = f"{folder}::{command}"
    app = _apps.get(key)
    if app is not None and app.alive():
        return f"يعمل مسبقاً (لم يُشغَّل مرة ثانية). الرابط: {getattr(app, 'url', '') or url or '؟'}\n{app.tail(15)}"
    app = bl.App(command, folder)
    _apps[key] = app
    deadline = time.time() + max(5, min(int(wait_seconds or 60), 300))
    while time.time() < deadline:
        if not app.alive():
            return f"توقف البرنامج فور تشغيله (رمز الخروج {app.proc.returncode}). مخرجاته:\n{app.tail(30)}"
        if kind == "desktop" and time.time() > deadline - max(5, int(wait_seconds or 60)) + 5:
            return f"بدأ برنامج سطح المكتب ({command}) ويعمل في الخلفية.\n{app.tail(15)}"
        found_url = url or app.url_in_output() or ""
        if found_url and bl.url_ready(found_url):
            app.url = found_url
            return f"يعمل في الخلفية: {found_url}\nالأمر: {command}\n{app.tail(15)}"
        time.sleep(0.5)
    return (f"بدأ في الخلفية لكنه لم يصبح جاهزاً خلال {wait_seconds} ثانية"
            f"{(' على ' + url) if url else ' (لم يظهر رابط في مخرجاته)'}. آخر مخرجاته:\n{app.tail(25)}")


@tool
def app_logs(lines: int = 40) -> str:
    """The latest output of the programs started with app_start (errors show up here)."""
    if not _apps:
        return "لا توجد برامج شُغّلت بـ app_start."
    out = []
    for key, app in _apps.items():
        state = "يعمل" if app.alive() else f"متوقف (رمز {app.proc.returncode})"
        out.append(f"== {key.split('::', 1)[1]} — {state} ==\n{app.tail(int(lines or 40))}")
    return "\n\n".join(out)


@tool
def app_stop(command: str = "") -> str:
    """Stop the program(s) started with app_start (all of them when `command` is empty)."""
    stopped = []
    for key, app in list(_apps.items()):
        if not command or key.endswith("::" + command):
            app.stop()
            stopped.append(key.split("::", 1)[1])
            _apps.pop(key, None)
    return ("أُوقف: " + "، ".join(stopped)) if stopped else "لا يوجد ما يُوقف."


def load_user_tools() -> None:
    folder = HERE / "tools"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"allinone_user_tools.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:  # a broken file must not take the other tools down
            log(f"[allinone] could not load {path.name}: {exc}")


if __name__ == "__main__":
    load_user_tools()
    serve("allinone", "1.0.0", instructions=(
        "Allinone tools: persistent memory shared across sessions (memory_save / memory_search), "
        "the active gateway's info and models, and ask_model to consult another model through the same gateway."))
