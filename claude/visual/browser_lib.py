"""Shared helpers for trying a program for real: start it, drive a browser, take screenshots.

Used by claude/visual/visual_check.py (Allinone's automatic visual check) and
claude/mcp/browser_server.py (browser tools for the model). Needs the
`playwright` Python package (installed by `allinone install visual`) for the
browser parts; screen capture and app start/stop use the standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

IS_WINDOWS = os.name == "nt"
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d+)?[^\s\"'<>)\]]*")


# ------------------------------------------------------------------ the program
class App:
    """A program started for testing; its output is kept so a crash can be explained."""

    def __init__(self, command: str, cwd: Path, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.lines: List[str] = []
        kwargs: Dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        full_env = dict(os.environ)
        full_env.update({"BROWSER": "none", "CI": "1", "PYTHONUNBUFFERED": "1", "FORCE_COLOR": "0"})
        full_env.update(env or {})
        self.proc = subprocess.Popen(command, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=full_env, **kwargs)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.lines.append(re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8", "replace").rstrip()))
            del self.lines[:-400]

    def alive(self) -> bool:
        return self.proc.poll() is None

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.lines[-n:])

    def url_in_output(self) -> Optional[str]:
        for line in self.lines:
            m = URL_RE.search(line)
            if m:
                return m.group(0).rstrip(".,;").replace("0.0.0.0", "localhost").replace("[::]", "localhost")
        return None

    def stop(self) -> None:
        if not self.alive():
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True, timeout=20)
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                self.proc.kill()
            except OSError:
                pass


def url_ready(url: str, timeout: float = 2.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def detect_app(cwd: Path, python: str = "") -> Optional[Dict[str, str]]:
    """How to start a project for testing, when the person did not say."""
    python = python or sys.executable
    pkg = cwd / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except (ValueError, OSError):
            scripts = {}
        for name in ("dev", "start", "serve", "preview"):
            if scripts.get(name):
                return {"command": f"npm run {name}", "url": "", "kind": "web"}
    if (cwd / "manage.py").is_file():
        port = free_port()
        return {"command": f'"{python}" manage.py runserver 127.0.0.1:{port} --noreload', "url": f"http://127.0.0.1:{port}/",
                "kind": "web"}
    for name in ("app.py", "main.py", "server.py", "run.py"):
        f = cwd / name
        if f.is_file():
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "streamlit" in src:
                port = free_port()
                return {"command": f'"{python}" -m streamlit run {name} --server.headless true --server.port {port}',
                        "url": f"http://localhost:{port}/", "kind": "web"}
            if re.search(r"Flask\(|FastAPI\(|uvicorn|gradio|\.run\(", src):
                return {"command": f'"{python}" {name}', "url": "", "kind": "web"}
            if re.search(r"tkinter|PyQt|PySide|wx\.App|kivy|pygame", src):
                return {"command": f'"{python}" {name}', "url": "", "kind": "desktop"}
    for folder in (cwd, cwd / "public", cwd / "dist", cwd / "build"):
        if (folder / "index.html").is_file():
            port = free_port()
            return {"command": f'"{python}" -m http.server {port} --bind 127.0.0.1 --directory "{folder}"',
                    "url": f"http://127.0.0.1:{port}/", "kind": "web"}
    return None


# ------------------------------------------------------------------ the screen
def capture_screen(path: Path) -> Optional[str]:
    """A screenshot of the whole screen, with what the system already has. Returns an error or None."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if IS_WINDOWS:
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                  "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($i);"
                  "$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);"
                  f"$i.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30)
        elif sys.platform == "darwin":
            subprocess.run(["screencapture", "-x", str(path)], capture_output=True, timeout=30)
        else:
            for cmd in (["gnome-screenshot", "-f", str(path)], ["grim", str(path)], ["scrot", "-o", str(path)],
                        ["import", "-window", "root", str(path)], ["spectacle", "-b", "-n", "-o", str(path)]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, capture_output=True, timeout=30)
                    if path.is_file():
                        break
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None if path.is_file() else "لا توجد أداة لالتقاط الشاشة على هذا النظام"


# ------------------------------------------------------------------ the browser
def find_browser() -> Optional[str]:
    cands = []
    if IS_WINDOWS:
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    for c in cands:
        if c.is_file():
            return str(c)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def launch(pw, browser_path: str = "", headless: bool = True):
    """Playwright's own Chromium if installed, otherwise Chrome/Edge already on the computer."""
    errors = []
    attempts = []
    if browser_path:
        attempts.append({"executable_path": browser_path})
    attempts.append({})
    found = find_browser()
    if found and found != browser_path:
        attempts.append({"executable_path": found})
    attempts += [{"channel": "msedge"}, {"channel": "chrome"}]
    for extra in attempts:
        try:
            return pw.chromium.launch(headless=headless, args=["--no-sandbox"] if not IS_WINDOWS else [], **extra)
        except Exception as exc:  # try the next browser
            errors.append(str(exc).splitlines()[0][:200])
    raise RuntimeError("لم يُعثر على متصفح لتجربة البرنامج. نفّذ: allinone install visual\n" + "\n".join(errors))


class PageWatch:
    """Collects what went wrong on a page: console errors, uncaught exceptions, failed requests."""

    def __init__(self, page, base_url: str = ""):
        self.console: List[str] = []
        self.page_errors: List[str] = []
        self.failed: List[str] = []
        self.base_host = re.sub(r"^https?://([^/]+).*$", r"\1", base_url) if base_url else ""
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)[:500]))
        page.on("requestfailed", lambda r: self._add_failed(r.url, (r.failure or "failed")))
        page.on("response", self._on_response)

    def _on_console(self, msg) -> None:
        # "Failed to load resource" has no URL here; failed requests are reported with their URL instead
        if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
            self.console.append(msg.text[:500])

    def _add_failed(self, url: str, why: Any) -> None:
        if url.endswith("/favicon.ico"):
            return
        if self.base_host and self.base_host not in url:
            return  # only the program's own requests
        self.failed.append(f"{why} {url}"[:300])

    def _on_response(self, resp) -> None:
        if resp.status >= 400:
            self._add_failed(resp.url, resp.status)

    def summary(self) -> Dict[str, List[str]]:
        dedupe = lambda xs: list(dict.fromkeys(xs))[:15]  # noqa: E731
        return {"console": dedupe(self.console), "page_errors": dedupe(self.page_errors), "failed": dedupe(self.failed)}


SNAPSHOT_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const items = [];
  for (const el of document.querySelectorAll('a,button,input,select,textarea,[role=button],[onclick],label,h1,h2,h3')) {
    if (!vis(el) || items.length >= 60) continue;
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.name || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    let sel = el.id ? '#' + el.id : (el.name ? `${tag}[name="${el.name}"]` : (text ? `text=${text.slice(0, 40)}` : tag));
    items.push({tag, type: el.type || '', text, selector: sel});
  }
  const body = document.body ? document.body.innerText.replace(/\\n{3,}/g, '\\n\\n').trim() : '';
  const media = document.querySelectorAll('img,canvas,svg,video').length;
  return {title: document.title, url: location.href, text: body.slice(0, 3000), textLength: body.length, media, items};
}"""


def snapshot(page) -> Dict[str, Any]:
    try:
        return page.evaluate(SNAPSHOT_JS)
    except Exception as exc:
        return {"title": "", "url": page.url, "text": "", "textLength": 0, "media": 0, "items": [], "error": str(exc)}


def snapshot_text(snap: Dict[str, Any]) -> str:
    lines = [f"العنوان: {snap.get('title')}", f"الرابط: {snap.get('url')}", "", "النص الظاهر:", snap.get("text") or "(فارغ)"]
    if snap.get("items"):
        lines += ["", "العناصر التفاعلية (استخدم المحدِّد selector مع browser_click / browser_type):"]
        lines += [f"- {i['tag']}{('/' + i['type']) if i.get('type') else ''}: {i['text'] or '—'}  →  {i['selector']}"
                  for i in snap["items"]]
    return "\n".join(lines)


def locate(page, target: str):
    """A selector (#id, .class, [..], text=, role=, xpath) or plain visible text."""
    t = target.strip()
    if re.match(r"^([#.\[/]|text=|role=|xpath=|css=|id=|//)", t) or re.match(r"^[a-z]+[#.\[]", t):
        return page.locator(t).first
    return page.get_by_text(t, exact=False).first


def is_blank(snap: Dict[str, Any]) -> bool:
    return (snap.get("textLength") or 0) < 3 and not snap.get("media") and not snap.get("items")
