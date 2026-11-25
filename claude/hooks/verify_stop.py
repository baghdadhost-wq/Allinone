#!/usr/bin/env python3
"""Stop hook: do not let Claude Code finish until the work is actually checked.

When the model says it is done, this hook runs the project's check command
(tests / build). If the check fails, it blocks the stop and hands the error
back, so Claude Code fixes it in the same session and tries again — like a
careful engineer would. After the same error repeats, it asks for a different
approach. It gives up after ALLINONE_VERIFY_ROUNDS failed checks.

Only active when Allinone sets ALLINONE_VERIFY=1 (the "verify & fix" switch).

Environment:
  ALLINONE_VERIFY          1 to enable
  ALLINONE_VERIFY_CMD      the check command; empty = detect from the project
  ALLINONE_VERIFY_DETECT   1 (default) to detect a command when none is given
  ALLINONE_VERIFY_ROUNDS   failed checks before giving up (default 3)
  ALLINONE_VERIFY_TIMEOUT  seconds per check (default 300)
  ALLINONE_VERIFY_STATE    json file where results are written for Allinone
Texts: config/prompts/verify_fix.txt, verify_change_approach.txt, verify_self_check.txt
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(os.environ.get("ALLINONE_ROOT") or Path(__file__).resolve().parents[2])

FIX = """تحقق Allinone من النتيجة بتشغيل: {cmd}
وفشل التحقق (رمز الخروج {code}، المحاولة {round} من {max}). آخر المخرجات:
```
{output}
```
لا تنهِ العمل بعد. اقرأ الخطأ كاملاً، حدّد سببه الجذري، أصلحه، ثم شغّل الأمر نفسه بنفسك لتتأكد أنه ينجح.
لا تعدّل الاختبارات أو تحذفها لتنجح زوراً، ولا تخفِ الخطأ."""

CHANGE = """⚠ الخطأ نفسه تكرر؛ الطريقة الحالية لم تنجح، فلا تكرر الإصلاح نفسه. غيّر الأسلوب:
1. اقرأ رسالة الخطأ كاملة وتتبّع مصدرها في الكود سطراً سطراً.
2. اختبر افتراضاتك بأوامر صغيرة (الإصدارات، المسارات، المدخلات، المتغيرات).
3. أضف طباعة أو تسجيلاً مؤقتاً لترى ما يحدث فعلاً.
4. ابحث في الكود عن استخدامات مشابهة تعمل، أو راجع التوثيق.
5. إن كان آخر تغيير هو سبب المشكلة فتراجع عنه وابدأ من نقطة سليمة.
6. جرّب حلاً بديلاً (طريقة أو مكتبة أخرى) أو بسّط المشكلة إلى أصغر حالة تفشل.
اذكر في سطر واحد الأسلوب الجديد الذي ستجربه، ثم نفّذه."""

SELF_CHECK = """قبل أن تنهي: لا يوجد أمر تحقق آلي لهذا المشروع، فتحقق بنفسك.
شغّل الكود أو الاختبارات أو البناء المناسب، وتأكد أن النتيجة تعمل فعلاً كما طُلب.
إذا ظهرت مشكلة فأصلحها وأعد التجربة، وإذا فشل الأسلوب نفسه مرتين فجرّب أسلوباً آخر.
ثم اختم بسطر يبدأ بـ «التحقق:» يذكر ما جرّبته والنتيجة."""

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _template(name: str, default: str) -> str:
    try:
        text = (ROOT / "config" / "prompts" / name).read_text(encoding="utf-8").strip()
        return text or default
    except OSError:
        return default


def detect_command(cwd: Path) -> Optional[str]:
    """A sensible check for common project types, only if the tool is installed."""
    pkg = cwd / "package.json"
    if pkg.is_file() and shutil.which("npm"):
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
        except (ValueError, OSError):
            scripts = {}
        test = str(scripts.get("test") or "")
        if test and "no test specified" not in test:
            return "npm test --silent"
        if scripts.get("build"):
            return "npm run build --silent"
    has_py_tests = (cwd / "tests").is_dir() or any(cwd.glob("test_*.py")) or (cwd / "pytest.ini").is_file()
    if has_py_tests:
        venv = next((p for p in (cwd / ".venv" / "Scripts" / "python.exe", cwd / ".venv" / "bin" / "python",
                                 cwd / "venv" / "Scripts" / "python.exe", cwd / "venv" / "bin" / "python") if p.is_file()), None)
        py = f'"{venv}"' if venv else ("python" if shutil.which("python") else "python3")
        if shutil.which("pytest") or venv:
            return f"{py} -m pytest -q -x"
        return f"{py} -m unittest discover -q" + (" -s tests" if (cwd / "tests").is_dir() else "")
    for marker, tool, cmd in (("Cargo.toml", "cargo", "cargo test -q"), ("go.mod", "go", "go test ./..."),
                              ("pom.xml", "mvn", "mvn -q test"), ("build.gradle", "gradle", "gradle test -q"),
                              ("build.gradle.kts", "gradle", "gradle test -q")):
        if (cwd / marker).is_file() and shutil.which(tool):
            return cmd
    if shutil.which("dotnet") and (any(cwd.glob("*.sln")) or any(cwd.glob("*.csproj"))):
        return "dotnet test"
    mk = cwd / "Makefile"
    if mk.is_file() and shutil.which("make"):
        try:
            if re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="replace"), re.M):
                return "make test"
        except OSError:
            pass
    return None


def edits_in_last_turn(transcript: str) -> bool:
    """Did Claude Code change files since the person's last message?"""
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return True  # unknown: be careful and check
    edited = False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if rec.get("type") == "user" and isinstance(content, str):
            edited = False  # a new request from the person
        elif rec.get("type") == "assistant" and isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS for b in content):
                edited = True
    return edited


def signature(output: str) -> str:
    """The gist of an error, ignoring numbers, times and paths, to notice the same failure again."""
    lines = [l for l in output.splitlines() if re.search(r"error|fail|exception|traceback|assert|خطأ", l, re.I)]
    gist = "\n".join((lines or output.splitlines())[-6:])
    gist = re.sub(r"0x[0-9a-f]+|\d+(\.\d+)?|[A-Za-z]:\\[^\s]+|/[^\s:]+", "#", gist)
    return re.sub(r"\s+", " ", gist).strip()[:500]


def run_check(cmd: str, cwd: Path, timeout: float) -> Dict[str, Any]:
    start = time.time()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        code = proc.returncode
        out = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        code = 124
        out = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else ""
        out += f"\n[توقف أمر التحقق بعد {int(timeout)} ثانية]"
    except OSError as exc:
        code, out = 127, str(exc)
    out = out.strip()
    return {"cmd": cmd, "code": code, "start": start, "end": time.time(),
            "output": out[-4000:] if len(out) > 4000 else out}


def load(path: Optional[str]) -> Dict[str, Any]:
    if path:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"rounds": 0, "checks": [], "self_check": False, "status": "none", "signatures": []}


def save(path: Optional[str], state: Dict[str, Any]) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def block(reason: str) -> int:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def main() -> int:
    if os.environ.get("ALLINONE_VERIFY") != "1":
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    state_path = os.environ.get("ALLINONE_VERIFY_STATE")
    state = load(state_path)
    cwd = Path(event.get("cwd") or os.getcwd())
    max_rounds = max(1, int(os.environ.get("ALLINONE_VERIFY_ROUNDS") or 3))
    timeout = min(850.0, float(os.environ.get("ALLINONE_VERIFY_TIMEOUT") or 300))
    cmd = (os.environ.get("ALLINONE_VERIFY_CMD") or "").strip()
    if not cmd and os.environ.get("ALLINONE_VERIFY_DETECT", "1") == "1":
        cmd = detect_command(cwd) or ""
    state["cmd"] = cmd

    if not cmd:
        # Nothing to run: ask the model once to test its own work, if it changed files.
        if not state.get("self_check") and edits_in_last_turn(event.get("transcript_path") or ""):
            state["self_check"], state["status"] = True, "self"
            save(state_path, state)
            return block(_template("verify_self_check.txt", SELF_CHECK))
        save(state_path, state)
        return 0

    if not state.get("checks") and not edits_in_last_turn(event.get("transcript_path") or ""):
        state["status"] = "skipped"  # nothing changed since the request: nothing to verify
        save(state_path, state)
        return 0

    check = run_check(cmd, cwd, timeout)
    check["round"] = len(state["checks"]) + 1
    state["checks"].append(check)
    if check["code"] == 0:
        state["status"] = "passed"
        save(state_path, state)
        return 0
    state["rounds"] = int(state.get("rounds", 0)) + 1
    if state["rounds"] >= max_rounds + 1:
        state["status"] = "failed"  # give up: let it stop; Allinone shows the failure
        save(state_path, state)
        return 0
    sig = signature(check["output"])
    repeated = sig in state.get("signatures", [])
    state.setdefault("signatures", []).append(sig)
    state["status"] = "fixing"
    save(state_path, state)
    reason = _template("verify_fix.txt", FIX)
    for key, val in (("cmd", cmd), ("code", check["code"]), ("round", state["rounds"]), ("max", max_rounds),
                     ("output", check["output"][-3000:] or "(لا توجد مخرجات)")):
        reason = reason.replace("{" + key + "}", str(val))
    if repeated or state["rounds"] >= 2:
        reason += "\n\n" + _template("verify_change_approach.txt", CHANGE)
    return block(reason)


if __name__ == "__main__":
    sys.exit(main())
