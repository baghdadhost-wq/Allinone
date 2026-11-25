#!/usr/bin/env python3
"""Try the program for real: start it, use it like a person, take screenshots at each stage.

    python visual_check.py config.json      → prints a JSON result

Config keys: cwd, app_command, app_url, kind (auto|web|desktop), steps, out_dir,
browser, start_timeout, max_pages, mobile, python.

Steps (one per line, optional — without steps it opens the start page and a few
of its links, at desktop and phone size):
    goto /login                    open a path or a full URL
    click دخول                     click a button/link by its text (or #id, .class, text=..., role=...)
    fill #email => me@example.com  type into a field
    press Enter                    press a key
    select #country => IQ          choose an option
    hover .menu                    move the mouse over something
    wait 2                         wait seconds
    expect تم الحفظ                this text must appear (a problem if it does not)
    screenshot after-login         take a named screenshot
A screenshot is taken after every step anyway.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_lib as bl  # noqa: E402


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "-", name, flags=re.U).strip("-")[:40] or "shot"


class Run:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.out = Path(cfg["out_dir"])
        self.out.mkdir(parents=True, exist_ok=True)
        self.shots: List[Dict[str, Any]] = []
        self.problems: List[str] = []
        self.notes: List[str] = []

    def shot_path(self, name: str) -> Path:
        return self.out / f"{len(self.shots) + 1:02d}-{_safe(name)}.png"

    # --------------------------------------------------------------- desktop
    def desktop(self, app: "bl.App") -> None:
        wait = float(self.cfg.get("desktop_wait") or 4)
        for i, label in enumerate(("start", "after-3s", "after-6s")):
            time.sleep(wait if i == 0 else 3)
            if not app.alive():
                self.problems.append(f"البرنامج أُغلق بعد التشغيل (رمز الخروج {app.proc.returncode}). آخر مخرجاته:\n{app.tail(25)}")
                break
            path = self.shot_path(label)
            err = bl.capture_screen(path)
            if err:
                self.notes.append(f"تعذّر التقاط الشاشة: {err}")
                break
            self.shots.append({"name": label, "path": str(path), "title": "الشاشة", "url": "", "text": ""})
        if re.search(r"Traceback|Exception|Error:", app.tail(60)):
            self.problems.append("ظهرت أخطاء في مخرجات البرنامج:\n" + app.tail(25))

    # --------------------------------------------------------------- web
    def web(self, url: str) -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = bl.launch(pw, self.cfg.get("browser") or "")
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
                page = ctx.new_page()
                page.set_default_timeout(float(self.cfg.get("step_timeout") or 10) * 1000)
                watch = bl.PageWatch(page, url)
                self.goto(page, url, "start")
                steps = [l.strip() for l in str(self.cfg.get("steps") or "").splitlines() if l.strip() and not l.strip().startswith("#")]
                if steps:
                    for line in steps:
                        self.step(page, url, line)
                else:
                    self.explore(page, url)
                if self.cfg.get("mobile", True):
                    mob = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                              ignore_https_errors=True).new_page()
                    bl.PageWatch(mob, url)
                    self.goto(mob, url, "mobile")
                s = watch.summary()
                if s["page_errors"]:
                    self.problems.append("أخطاء JavaScript غير معالجة في الصفحة:\n- " + "\n- ".join(s["page_errors"]))
                if s["console"]:
                    self.problems.append("أخطاء في console المتصفح:\n- " + "\n- ".join(s["console"]))
                if s["failed"]:
                    self.problems.append("طلبات فشلت (روابط أو ملفات أو API لا تعمل):\n- " + "\n- ".join(s["failed"]))
            finally:
                browser.close()

    def capture(self, page, name: str) -> Dict[str, Any]:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        snap = bl.snapshot(page)
        path = self.shot_path(name)
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            self.notes.append(f"تعذّرت الصورة {name}: {exc}")
            return snap
        self.shots.append({"name": name, "path": str(path), "title": snap.get("title") or "", "url": snap.get("url") or "",
                           "text": (snap.get("text") or "")[:400]})
        if bl.is_blank(snap):
            self.problems.append(f"الصفحة فارغة تماماً في مرحلة «{name}» ({snap.get('url')}).")
        return snap

    def goto(self, page, url: str, name: str) -> Dict[str, Any]:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is not None and resp.status >= 400:
                self.problems.append(f"فتح {url} أعاد الحالة {resp.status}.")
        except Exception as exc:
            self.problems.append(f"تعذّر فتح {url}: {str(exc).splitlines()[0]}")
        return self.capture(page, name)

    def explore(self, page, base: str) -> None:
        """No scenario given: visit a few of the program's own pages."""
        try:
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        except Exception:
            hrefs = []
        host = urlparse(base).netloc
        seen = {urlparse(page.url)._replace(fragment="").geturl()}
        for h in hrefs:
            if len(seen) >= int(self.cfg.get("max_pages") or 4):
                break
            if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(page.url, h)
            key = urlparse(full)._replace(fragment="").geturl()
            if urlparse(full).netloc != host or key in seen:
                continue
            seen.add(key)
            self.goto(page, full, "page-" + (urlparse(full).path.strip("/") or "home"))

    def step(self, page, base: str, line: str) -> None:
        verb, _, rest = line.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        target, _, value = rest.partition("=>")
        target, value = target.strip(), value.strip()
        try:
            if verb == "goto":
                self.goto(page, urljoin(base, rest), "goto-" + rest)
                return
            if verb == "click":
                bl.locate(page, target).click()
            elif verb in ("fill", "type"):
                bl.locate(page, target).fill(value)
            elif verb == "press":
                page.keyboard.press(rest)
            elif verb == "select":
                bl.locate(page, target).select_option(value)
            elif verb == "hover":
                bl.locate(page, target).hover()
            elif verb == "wait":
                time.sleep(min(60.0, float(rest or 1)))
            elif verb == "expect":
                try:
                    page.get_by_text(rest, exact=False).first.wait_for(state="visible", timeout=8000)
                except Exception:
                    self.problems.append(f"كان متوقعاً أن يظهر النص «{rest}» لكنه لم يظهر.")
            elif verb == "screenshot":
                self.capture(page, rest or "shot")
                return
            else:
                self.notes.append(f"خطوة غير مفهومة: {line}")
                return
        except Exception as exc:
            self.problems.append(f"فشلت الخطوة «{line}»: {str(exc).splitlines()[0][:300]}")
        self.capture(page, f"{verb}-{target or rest}")


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run = Run(cfg)
    started = time.time()
    cwd = Path(cfg.get("cwd") or ".")
    command, url, kind = (cfg.get("app_command") or "").strip(), (cfg.get("app_url") or "").strip(), cfg.get("kind") or "auto"
    if not command and not url:
        found = bl.detect_app(cwd, cfg.get("python") or "")
        if found:
            command, url = found["command"], found["url"]
            kind = found["kind"] if kind == "auto" else kind
            run.notes.append(f"اكتُشف أمر التشغيل تلقائياً: {command}")
    if kind == "auto":
        kind = "web" if (url or command) else "none"
    app = None
    result: Dict[str, Any] = {"kind": kind, "command": command, "url": url}
    try:
        if kind == "none":
            result["skipped"] = "لم يُعرف كيف يُشغَّل البرنامج؛ حدّد «أمر تشغيل البرنامج» في المشروع."
        else:
            if command:
                app = bl.App(command, cwd)
            if kind == "desktop":
                run.desktop(app) if app else run.notes.append("لا يوجد أمر تشغيل لبرنامج سطح المكتب.")
            else:
                deadline = time.time() + float(cfg.get("start_timeout") or 90)
                while time.time() < deadline:
                    if app is not None and not app.alive():
                        run.problems.append(f"البرنامج توقف عند التشغيل (رمز الخروج {app.proc.returncode}). آخر مخرجاته:\n"
                                            + app.tail(30))
                        break
                    if not url and app is not None:
                        url = app.url_in_output() or ""
                    if url and bl.url_ready(url):
                        break
                    time.sleep(0.5)
                else:
                    run.problems.append(f"البرنامج لم يصبح جاهزاً خلال {int(cfg.get('start_timeout') or 90)} ثانية"
                                        + (f" على {url}" if url else " (لم يظهر رابط في مخرجاته)") + ".\n"
                                        + (app.tail(20) if app else ""))
                result["url"] = url
                if url and not run.problems:
                    run.web(url)
                if app is not None and not app.alive() and not run.problems:
                    run.problems.append("البرنامج توقف أثناء التجربة. آخر مخرجاته:\n" + app.tail(25))
    except Exception as exc:
        run.notes.append(f"تعذّرت التجربة المرئية: {exc}")
        result["error"] = str(exc)
    finally:
        if app is not None:
            result["app_log"] = app.tail(40)
            app.stop()
    result.update(ok=not run.problems, problems=run.problems, shots=run.shots, notes=run.notes,
                  seconds=round(time.time() - started, 2))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
