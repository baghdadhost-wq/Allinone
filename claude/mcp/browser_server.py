#!/usr/bin/env python3
"""Allinone browser tools: let the model try the program the way a person would.

Like Claude's own browser use: open the program, read what is on the page,
click, type, take screenshots at each stage, and read the console errors.
Started by Allinone when "👁 Visual test" is on, with the Python that has
Playwright (`allinone install visual`).

  browser_open(url)                 open a page; returns what is on it
  browser_snapshot()                the page as text: title, visible text, buttons/fields + selectors
  browser_click(target)             click by visible text or selector
  browser_type(target, text, submit) type into a field
  browser_press(key)                press a key (Enter, Tab, Escape...)
  browser_screenshot(name, full)    save a screenshot (returned as an image to models that can see)
  browser_console()                 JavaScript errors and failed requests so far
  browser_resize(width, height)     e.g. 390x844 to check the phone layout
  browser_close()
  screen_capture(name)              screenshot of the whole screen (desktop programs)
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "visual"))

from mcp_kit import serve, tool  # noqa: E402
import browser_lib as bl  # noqa: E402

OUT = Path(os.environ.get("ALLINONE_VISUAL_OUT") or (HERE.parent.parent / "data" / "visual" / "manual"))
BROWSER = os.environ.get("ALLINONE_BROWSER", "")
SEND_IMAGES = os.environ.get("ALLINONE_BROWSER_IMAGES", "1") == "1"
CFG = {"shot_scale": os.environ.get("ALLINONE_SHOT_SCALE") or 0.75, "jpeg_quality": os.environ.get("ALLINONE_JPEG_QUALITY") or 60}

_state = {"pw": None, "browser": None, "page": None, "watch": None, "n": 0}


def _page():
    if _state["page"] is None:
        from playwright.sync_api import sync_playwright
        _state["pw"] = sync_playwright().start()
        _state["browser"] = bl.launch(_state["pw"], BROWSER)
        ctx = _state["browser"].new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True,
                                            device_scale_factor=bl.shot_scale(CFG))
        _state["page"] = ctx.new_page()
        _state["page"].set_default_timeout(15000)
        _state["watch"] = bl.PageWatch(_state["page"])
    return _state["page"]


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass


def _shot(name: str, full: bool = False, screen: bool = False):
    _state["n"] += 1
    path = OUT / f"{_state['n']:02d}-{re.sub(r'[^\w-]+', '-', name or 'shot', flags=re.U)[:40]}.jpg"
    OUT.mkdir(parents=True, exist_ok=True)
    if screen:
        err = bl.capture_screen(path, max_width=int(float(CFG["shot_scale"]) * 1280))
        if err:
            raise RuntimeError(err)
        path = path if path.is_file() else path.with_suffix(".png")
    else:
        bl.take_shot(_page(), path, CFG, full=full)
    cost = bl.tokens_estimate(path)
    content = [{"type": "text", "text": f"حُفظت الصورة: {path} (≈{cost} توكن عند قراءتها)"}]
    if SEND_IMAGES and path.stat().st_size < 4_500_000:
        mime = "image/jpeg" if path.suffix.lower() == ".jpg" else "image/png"
        content.append({"type": "image", "data": base64.b64encode(path.read_bytes()).decode(), "mimeType": mime})
    return {"_content": content}


def _errors_since(before) -> str:
    s = _state["watch"].summary() if _state["watch"] else {"console": [], "page_errors": [], "failed": []}
    lines = []
    for key, label in (("page_errors", "خطأ JavaScript"), ("console", "console"), ("failed", "طلب فشل")):
        for item in s[key][before.get(key, 0):]:
            lines.append(f"⚠ {label}: {item}")
    return "\n".join(lines)


def _counts():
    s = _state["watch"].summary() if _state["watch"] else {}
    return {k: len(v) for k, v in s.items()}


def _report(page, before) -> str:
    _settle(page)
    text = bl.snapshot_text(bl.snapshot(page))
    errs = _errors_since(before)
    return text + ("\n\n" + errs if errs else "")


@tool
def browser_open(url: str) -> str:
    """Open a page of the program (e.g. http://localhost:5173/). Start the program first (in the background).
    Returns the page as text: title, visible text and the buttons/fields with selectors to use next."""
    page = _page()
    before = _counts()
    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    status = f"الحالة: {resp.status}\n" if resp is not None else ""
    return status + _report(page, before)


@tool
def browser_snapshot() -> str:
    """The current page as text: title, URL, visible text, and clickable/typeable elements with selectors."""
    page = _page()
    return _report(page, _counts())


@tool
def browser_click(target: str) -> str:
    """Click a button or link by its visible text, or by a selector (#id, .class, text=..., role=button[name=...])."""
    page = _page()
    before = _counts()
    bl.locate(page, target).click()
    return _report(page, before)


@tool
def browser_type(target: str, text: str, submit: bool = False) -> str:
    """Type into a field found by its label/placeholder text or selector. submit=true presses Enter afterwards."""
    page = _page()
    before = _counts()
    loc = bl.locate(page, target)
    try:
        loc.fill(text)
    except Exception:
        page.get_by_placeholder(target).first.fill(text)
    if submit:
        page.keyboard.press("Enter")
    return _report(page, before)


@tool
def browser_press(key: str) -> str:
    """Press a key on the page: Enter, Tab, Escape, ArrowDown..."""
    page = _page()
    before = _counts()
    page.keyboard.press(key)
    return _report(page, before)


@tool
def browser_screenshot(name: str = "", full: bool = False):
    """Take a screenshot of what is visible now (full=true for the whole page, up to 3 screens; costs more).
    Take one at each stage you test. Pictures are small to keep reading them cheap.
    The file path is returned; models that can see images also receive the picture."""
    _page()
    _settle(_state["page"])
    return _shot(name or "page", full=full)


@tool
def browser_console() -> str:
    """JavaScript errors, console errors and failed requests seen since the browser opened."""
    s = _state["watch"].summary() if _state["watch"] else {}
    if not any(s.values()):
        return "لا توجد أخطاء مسجلة."
    out = []
    for key, label in (("page_errors", "أخطاء JavaScript"), ("console", "أخطاء console"), ("failed", "طلبات فشلت")):
        if s.get(key):
            out += [label + ":"] + [f"- {x}" for x in s[key]]
    return "\n".join(out)


@tool
def browser_resize(width: int, height: int) -> str:
    """Change the window size, e.g. 390x844 to check the phone layout, 1280x800 for desktop."""
    page = _page()
    page.set_viewport_size({"width": int(width), "height": int(height)})
    return f"الحجم الآن {width}x{height}. خذ صورة بـ browser_screenshot لترى التخطيط."


@tool
def browser_close() -> str:
    """Close the browser."""
    for key in ("browser",):
        try:
            if _state[key]:
                _state[key].close()
        except Exception:
            pass
    try:
        if _state["pw"]:
            _state["pw"].stop()
    except Exception:
        pass
    _state.update(pw=None, browser=None, page=None, watch=None)
    return "أُغلق المتصفح."


@tool
def screen_capture(name: str = "screen", wait_seconds: float = 0):
    """Screenshot of the whole screen — for desktop programs (start the program first, then wait a moment)."""
    if wait_seconds:
        time.sleep(min(30.0, float(wait_seconds)))
    return _shot(name, screen=True)


if __name__ == "__main__":
    serve("allinone_browser", "1.0.0",
          "Browser tools to try the program for real: open it, click, type, take screenshots, read console errors.")
