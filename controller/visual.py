"""Visual testing support: the Python that has Playwright, and installing it.

`allinone install visual` creates data/tools/visual (a Python virtual environment)
with the Playwright package. A browser is downloaded only if the computer has no
Chrome or Edge (Windows always has Edge). Nothing here is needed unless you use
"👁 Visual test".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from . import ui
from .i18n import tr

IS_WINDOWS = os.name == "nt"
_cache: Dict[str, Optional[str]] = {}


def venv_dir(ctx) -> Path:
    return ctx.data_dir / "tools" / "visual"


def venv_python(ctx) -> Path:
    d = venv_dir(ctx)
    return d / "Scripts" / "python.exe" if IS_WINDOWS else d / "bin" / "python"


def _has_playwright(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "import playwright.sync_api"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_for_visual(ctx, refresh: bool = False) -> Optional[str]:
    """The Python to run the visual tools with: configured, Allinone's own environment, or the current one."""
    if not refresh and "py" in _cache:
        return _cache["py"]
    cfg = (ctx.cfg.get("gui") or {}).get("visual") or {}
    found = None
    for cand in (str(cfg.get("python") or ""), str(venv_python(ctx)), sys.executable):
        if cand and Path(cand).exists() and _has_playwright(cand):
            found = cand
            break
    _cache["py"] = found
    return found


def browser_path(ctx) -> str:
    cfg = (ctx.cfg.get("gui") or {}).get("visual") or {}
    if cfg.get("browser"):
        return str(cfg["browser"])
    sys.path.insert(0, str(ctx.cfg.root / "claude" / "visual"))
    try:
        import browser_lib  # type: ignore
        return browser_lib.find_browser() or ""
    except Exception:
        return ""


def status(ctx) -> Dict[str, object]:
    py = python_for_visual(ctx)
    return {"available": bool(py), "python": py or "", "browser": browser_path(ctx) if py else ""}


def install(ctx, force: bool = False, log=None) -> bool:
    """Create the environment, install Playwright, and a browser only when none is on the computer."""
    say = log or (lambda msg: ui.say(msg))
    d = venv_dir(ctx)
    py = venv_python(ctx)
    if force or not py.exists():
        say(tr("Creating the Python environment for visual testing…", "إنشاء بيئة Python للتجربة المرئية…"))
        res = subprocess.run([sys.executable, "-m", "venv", str(d)], capture_output=True, text=True)
        if res.returncode != 0:
            say(res.stdout + res.stderr)
            return False
    say(tr("Installing Playwright (browser automation)…", "تثبيت Playwright (التحكم بالمتصفح)…"))
    res = subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "playwright"], capture_output=True, text=True)
    say((res.stdout + res.stderr)[-1500:])
    if res.returncode != 0:
        return False
    _cache.pop("py", None)
    if not browser_path(ctx):
        say(tr("No Chrome/Edge found — downloading Chromium…", "لم يُعثر على Chrome أو Edge — تنزيل Chromium…"))
        res = subprocess.run([str(py), "-m", "playwright", "install", "chromium"], capture_output=True, text=True)
        say((res.stdout + res.stderr)[-1500:])
        if res.returncode != 0:
            return False
    ok = bool(python_for_visual(ctx, refresh=True))
    say(tr("Visual testing is ready.", "التجربة المرئية جاهزة.") if ok else
        tr("Visual testing is not ready.", "التجربة المرئية غير جاهزة."))
    return ok
