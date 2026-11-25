"""Allinone GUI — a Claude-style chat in your browser, served only on 127.0.0.1.

Every message is executed by Claude Code (`claude -p --output-format stream-json`)
through the gateway you pick: OmniRoute, FreeLLMAPI, or Claude itself.

Security: the server listens on 127.0.0.1 only, checks the Host header (blocks DNS
rebinding) and requires a random per-launch token on every API call, so other web
pages cannot drive Claude Code on your computer.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__, flows, i18n, procs, ui
from .claude import ClaudeError
from .gateways import GatewayError
from .i18n import tr
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"
DEEP_PLACEHOLDER = "[ينقل السؤال هنا]"
PERMISSION_MODES = ["acceptEdits", "auto", "plan", "bypassPermissions", "default"]
EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]
MAX_JSON = 2 * 1024 * 1024
DEFAULT_UPLOAD_MB = 200


# ------------------------------------------------------------ prompt building
def read_prompt(ctx, name: str, fallback: str) -> str:
    path = ctx.cfg.root / "config" / "prompts" / name
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or fallback
    except OSError:
        return fallback


def apply_deep(template: str, question: str) -> str:
    if DEEP_PLACEHOLDER in template:
        return template.replace(DEEP_PLACEHOLDER, question)
    return template.rstrip() + "\n\n" + question


def transcript_text(messages: Iterable[Dict[str, Any]]) -> str:
    parts = []
    for m in messages:
        if m["role"] == "user":
            parts.append(f"المستخدم:\n{m['display']}")
        else:
            meta = m.get("meta") or {}
            who = "Claude" + (f" ({meta.get('gateway_title') or meta.get('gateway')}"
                              + (f" · {meta.get('model')}" if meta.get("model") else "") + ")" if meta.get("gateway") else "")
            parts.append(f"{who}:\n{m['display']}")
    return "\n\n---\n\n".join(parts)


REPORT_HEADER = """أنت خبير برمجيات. هذا تقرير من أداة Allinone: محادثة نفّذ فيها Claude Code مهمة ولم يصل إلى نتيجة صحيحة.
يحتوي التقرير على طلب المستخدم، والخطوات والأوامر التي جرّبها Claude Code مع نتائجها، والأخطاء التي ظهرت.
المطلوب منك:
1. حدّد السبب الجذري للمشكلة.
2. اشرح لماذا لم تنجح الطرق السابقة.
3. أعطِ حلاً عملياً خطوة بخطوة (أوامر أو تعديلات كود كاملة) يستطيع Claude Code تنفيذه مباشرة.
"""

EXPERT_TEMPLATE = """هذه إجابة من مستشار خارجي (نموذج آخر) على المشكلة في هذه المحادثة.
اقرأها بعناية، وتحقق منها مقابل الملفات الفعلية، ثم نفّذ الحل المناسب خطوة بخطوة.
إذا وجدت فيها خطأ أو شيئاً لا ينطبق على مشروعنا فوضّح ذلك واستخدم الأصح.
{note}
----- بداية الإجابة -----
{answer}
----- نهاية الإجابة -----"""

REPORT_ASK = ("اقرأ تقرير الأخطاء المرفق. حدّد السبب الجذري، واشرح لماذا فشلت الطرق السابقة، "
              "ثم أعطني حلاً كاملاً خطوة بخطوة.")


def _clip(text: Any, limit: int) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1, default=str)
    s = s or ""
    return s if len(s) <= limit else s[:limit] + f"\n… [قُطع {len(s) - limit} حرفاً]"


def _part_is_error(p: Dict[str, Any]) -> bool:
    return bool(p.get("is_error") or p.get("error"))


def build_report_text(conv: Dict[str, Any], messages: List[Dict[str, Any]], header: str,
                      project: Optional[Dict[str, Any]] = None) -> str:
    """A plain-text report of what was asked, every step Claude Code took, and every error."""
    out = [header.strip(), "", "=" * 70, "معلومات المحادثة", "=" * 70,
           f"العنوان: {conv.get('title') or ''}",
           f"مجلد العمل: {conv.get('project') or ''}",
           f"آخر بوابة/نموذج: {conv.get('gateway') or ''} {('· ' + conv['model']) if conv.get('model') else ''}",
           f"التاريخ: {time.strftime('%Y-%m-%d %H:%M')}"]
    if project:
        out.append(f"المشروع: {project.get('name')}")
        if project.get("instructions"):
            out += ["تعليمات المشروع:", _clip(project["instructions"], 3000)]
    errors: List[str] = []
    turn = 0
    for m in messages:
        meta = m.get("meta") or {}
        if m["role"] == "user":
            turn += 1
            out += ["", "=" * 70, f"الدور {turn} — طلب المستخدم", "=" * 70, _clip(m.get("display") or "", 6000)]
            if meta.get("attachments"):
                out.append("الملفات المرفقة: " + ", ".join(meta["attachments"]))
            if meta.get("expert"):
                out += ["(هذه الرسالة إجابة خارجية رُفعت إلى المحادثة:)", _clip(m.get("sent") or "", 6000)]
            continue
        who = meta.get("gateway_title") or meta.get("gateway") or "Claude"
        out += ["", f"--- رد Claude Code عبر {who}{(' · ' + meta['model']) if meta.get('model') else ''} ---"]
        step = 0
        for p in meta.get("parts") or [{"kind": "text", "text": m.get("display") or ""}]:
            if p.get("kind") == "tool":
                step += 1
                bad = _part_is_error(p)
                out += [f"[الخطوة {step}] {'✘ فشلت' if bad else '✔'} الأداة: {p.get('name')}",
                        "  المدخلات:", "  " + _clip(p.get("input"), 2500).replace("\n", "\n  ")]
                if p.get("result") is not None:
                    out += ["  النتيجة:", "  " + _clip(p.get("result"), 2500).replace("\n", "\n  ")]
                if bad:
                    errors.append(f"الدور {turn}، الخطوة {step} ({p.get('name')}): " + _clip(p.get("result") or "", 600))
            elif p.get("kind") == "text" and (p.get("text") or "").strip():
                if _part_is_error(p):
                    out += ["[خطأ]", _clip(p["text"], 3000)]
                    errors.append(f"الدور {turn}: " + _clip(p["text"], 600))
                else:
                    out += ["[نص الرد]", _clip(p["text"], 4000)]
        for a in meta.get("approvals") or []:
            state = {"allow": "سُمح", "deny": "رُفض"}.get(a.get("behavior"), a.get("behavior"))
            out.append(f"[إذن] {a.get('tool')}: {state}{' (إذن مسبق)' if a.get('auto') else ''}")
        if meta.get("is_error") and not any(_part_is_error(p) for p in meta.get("parts") or []):
            errors.append(f"الدور {turn}: انتهى الرد بحالة خطأ.")
        bits = [f"{meta['duration_ms'] / 1000:.1f} ث" if meta.get("duration_ms") else "",
                f"{meta['num_turns']} دورات" if meta.get("num_turns") else "",
                "أُوقف يدوياً" if meta.get("stopped") else ""]
        if any(bits):
            out.append("(" + " · ".join(b for b in bits if b) + ")")
    out += ["", "=" * 70, f"ملخص الأخطاء ({len(errors)})", "=" * 70]
    out += [f"{i}. {e}" for i, e in enumerate(errors, 1)] or ["لم تُسجَّل أخطاء صريحة؛ راجع الخطوات أعلاه."]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ interruptions
CONTINUE_TEMPLATE = """انقطع تنفيذ المهمة السابقة قبل أن يكتمل (السبب: {reason}).
تابع من النقطة التي توقفت عندها بالضبط:
1. راجع آخر ما فعلته في هذه المحادثة، وتحقق من الحالة الفعلية للملفات في المجلد (لا تعتمد على الذاكرة وحدها).
2. لا تُعِد خطوات اكتملت فعلاً، وأصلح أي ملف تركته نصف مكتوب.
3. أكمل المهمة الأصلية حتى النهاية، ثم لخّص باختصار ما أُنجز وما بقي.
إذا كانت المهمة قد اكتملت فعلاً فقل ذلك واذكر الدليل."""

HANDOFF_TEMPLATE = """هذه مهمة بدأت في جلسة سابقة (ربما على بوابة أو نموذج آخر) وانقطعت. السبب: {reason}.
فيما يلي ملف التسليم الذي يلخص ما طُلب وما أُنجز (محفوظ أيضاً في {file}).
قبل أي عمل: تحقق من الحالة الفعلية للملفات في المجلد، ولا تكرر ما اكتمل، وأصلح ما بقي ناقصاً.

{handoff}

----- المطلوب الآن -----
{message}"""

RESUME_LABEL = "▶ متابعة"
AUTO_RESUME_LABEL = "↻ متابعة تلقائية ({n}/{m})"

# reason -> (title, retry automatically, wait seconds, how to resume, attempts)
_REASONS = {
    "closed": ("أُغلق Allinone أو توقف الجهاز أثناء العمل", False, 0, "continue", 0),
    "stopped": ("أوقفتَ الرد يدوياً", False, 0, "continue", 0),
    "gateway_down": ("البوابة توقفت أو لا تستجيب", True, 8, "continue", 3),
    "rate_limit": ("وصل المزوّد إلى حد الطلبات", True, 60, "continue", 3),
    "usage_limit": ("وصل حساب Claude إلى حد الاستخدام", True, 0, "continue", 1),
    "network": ("انقطاع في الشبكة أو ضغط مؤقت على الخادم", True, 15, "continue", 3),
    "auth": ("مشكلة في تسجيل الدخول أو المفتاح", False, 0, "continue", 0),
    "context": ("المحادثة أطول من سياق النموذج", True, 0, "handoff", 1),
    "session_missing": ("جلسة Claude Code السابقة غير موجودة", True, 0, "handoff", 1),
    "max_turns": ("انتهى عدد الدورات المسموح", True, 0, "continue", 2),
    "crash": ("خرج Claude Code بخطأ غير متوقع", True, 10, "continue", 1),
}

_ADVICE = {
    ("gateway_down", "omniroute"): "سيعيد Allinone تشغيل OmniRoute ثم يتابع من الجلسة نفسها. إن تكرر فافتح لوحة التحكم وتحقق من المزوّدين.",
    ("gateway_down", "freellmapi"): "سيعيد Allinone تشغيل FreeLLMAPI ثم يتابع. إن تكرر فتحقق من سجله ومن وجود مفاتيح مزوّدين.",
    ("gateway_down", ""): "سيعيد Allinone تشغيل البوابة ثم يتابع من الجلسة نفسها.",
    ("rate_limit", "omniroute"): "كل المزوّدين في الـcombo وصلوا الحد. سينتظر ويعيد المحاولة؛ وللحل الدائم أضف مزوّداً احتياطياً (fallback) في لوحة OmniRoute، أو اختر بوابة أخرى ثم «متابعة».",
    ("rate_limit", "freellmapi"): "استُنفدت المفاتيح المجانية المتاحة مؤقتاً. سينتظر ويعيد المحاولة؛ وللحل الدائم أضف مفاتيح مزوّدين أكثر في FreeLLMAPI، أو اختر بوابة أخرى ثم «متابعة».",
    ("rate_limit", "anthropic"): "حد طلبات مؤقت من Anthropic. سينتظر ويعيد المحاولة.",
    ("rate_limit", ""): "سينتظر ويعيد المحاولة، أو اختر بوابة أخرى ثم «متابعة».",
    ("usage_limit", "anthropic"): "سيُستأنف تلقائياً عند تجدد الحد إن بقيت نافذة Allinone مفتوحة. أو اختر OmniRoute أو FreeLLMAPI من الأعلى ثم «متابعة» لتكمل الآن بملف تسليم.",
    ("auth", "anthropic"): "سجّل الدخول من زر «تسجيل الدخول» ثم اضغط «متابعة».",
    ("auth", "omniroute"): "مفتاح OmniRoute غير مقبول. نفّذ: allinone key omniroute (أو افتح لوحة التحكم) ثم «متابعة».",
    ("auth", "freellmapi"): "مفتاح FreeLLMAPI غير مقبول. نفّذ: allinone key freellmapi ثم «متابعة».",
    ("auth", ""): "صحّح المفتاح أو تسجيل الدخول ثم اضغط «متابعة».",
    ("context", ""): "سيبدأ جلسة جديدة بملف تسليم يلخص العمل السابق ويتابع منها.",
    ("session_missing", ""): "سيبدأ جلسة جديدة بملف تسليم يلخص العمل السابق ويتابع منها.",
    ("closed", ""): "اضغط «متابعة» ليكمل Claude Code من الجلسة نفسها؛ ما حُفظ قبل الانقطاع ظاهر أعلاه.",
    ("stopped", ""): "اضغط «متابعة» متى أردت ليكمل من حيث توقف.",
    ("network", ""): "سيعيد المحاولة تلقائياً بعد لحظات.",
    ("max_turns", ""): "سيتابع تلقائياً في دورة جديدة.",
    ("crash", ""): "سيعيد المحاولة مرة؛ إن تكرر فأنشئ 🧾 تقرير الأخطاء.",
}


def _parse_reset(text: str, now: Optional[float] = None) -> Optional[float]:
    """When does a Claude usage limit reset? Understands `...limit reached|1735689600` and `resets 3pm`."""
    now = time.time() if now is None else now
    m = re.search(r"limit reached\|(\d{9,})", text)
    if m:
        return float(m.group(1))
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, int(m.group(2) or 0), 0, 0, 0, -1))
        return target + 86400 if target <= now else target
    return None


def diagnose(gw_type: str, text: str, gw_up: bool = True, stopped: bool = False, subtype: str = "",
             closed: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Why did a run stop, and how should it be resumed? Different gateways fail differently."""
    low = (text or "").lower()
    if closed:
        reason = "closed"
    elif stopped:
        reason = "stopped"
    elif "no conversation found" in low or "session not found" in low:
        reason = "session_missing"
    elif subtype == "error_max_turns" or "max turns" in low or "maximum number of turns" in low:
        reason = "max_turns"
    elif re.search(r"prompt is too long|context (length|window)|maximum context|too many tokens|context_length", low):
        reason = "context"
    elif re.search(r"usage limit|limit reached|hit your limit|5-hour limit|weekly limit", low) and gw_type == "anthropic":
        reason = "usage_limit"
    elif re.search(r"\b401\b|\b403\b|unauthori[sz]ed|invalid (api )?key|authentication|/login|not logged in|"
                   r"oauth token|token (has )?expired|غير مسجّل", low):
        reason = "auth"
    elif re.search(r"\b429\b|rate.?limit|too many requests|quota|exhausted|no available (provider|key|model)|"
                   r"all providers|usage limit|limit reached", low):
        reason = "rate_limit"
    elif not gw_up and gw_type != "anthropic":
        reason = "gateway_down"
    elif re.search(r"econnrefused|econnreset|etimedout|enotfound|fetch failed|socket hang up|network|timed? ?out|"
                   r"\b5\d\d\b|overloaded|bad gateway|service unavailable|connection (error|refused|reset)", low):
        reason = "network" if gw_up or gw_type == "anthropic" else "gateway_down"
    else:
        reason = "crash"
    title, retry, wait, mode, attempts = _REASONS[reason]
    gtype = gw_type if gw_type in ("omniroute", "freellmapi", "anthropic") else ""
    advice = _ADVICE.get((reason, gtype)) or _ADVICE.get((reason, "")) or ""
    resume_at = None
    if reason == "usage_limit":
        resume_at = _parse_reset(text, now)
        if resume_at is None:
            retry = False
            advice = ("لم يُعرف وقت تجدد الحد. اختر OmniRoute أو FreeLLMAPI من الأعلى ثم «متابعة» لتكمل الآن، "
                      "أو اضغط «متابعة» لاحقاً على Claude.")
        else:
            wait = max(0, int(resume_at - (time.time() if now is None else now)) + 30)
    return {"reason": reason, "title": title, "advice": advice, "retry": retry, "wait": wait, "mode": mode,
            "attempts": attempts, "resume_at": resume_at, "detail": (text or "")[-600:]}


_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def build_handoff_text(conv: Dict[str, Any], messages: List[Dict[str, Any]], reason: str,
                       project: Optional[Dict[str, Any]] = None) -> str:
    """A compact hand-over note so a fresh session (or another gateway/model) can finish the work."""
    users = [m for m in messages if m["role"] == "user" and not (m.get("meta") or {}).get("resume")]
    out = ["# ملف تسليم المهمة", "",
           f"- المحادثة: {conv.get('title') or ''}", f"- مجلد العمل: {conv.get('project') or ''}",
           f"- سبب الانقطاع: {reason}", f"- الوقت: {time.strftime('%Y-%m-%d %H:%M')}"]
    if project:
        out.append(f"- المشروع: {project.get('name')}")
    if users:
        out += ["", "## الهدف (الطلب الأول)", _clip(users[0].get("display") or "", 3000)]
        if len(users) > 1:
            out += ["", "## الطلبات اللاحقة بالترتيب"]
            out += [f"{i}. {_clip(m.get('display') or '', 1200)}" for i, m in enumerate(users[1:], 2)]
    todos, files, commands, errors = None, {}, [], []
    last_text = ""
    for m in messages:
        if m["role"] != "assistant":
            continue
        for p in (m.get("meta") or {}).get("parts") or []:
            if p.get("kind") == "tool":
                inp = p.get("input") if isinstance(p.get("input"), dict) else {}
                bad = _part_is_error(p)
                if p.get("name") == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todos = inp["todos"]
                elif p.get("name") in _EDIT_TOOLS and inp.get("file_path"):
                    files[inp["file_path"]] = "✘ فشل" if bad else ("✔" if p.get("result") is not None else "… لم يكتمل")
                elif p.get("name") == "Bash" and inp.get("command"):
                    commands.append(("✘" if bad else "✔" if p.get("result") is not None else "…") + " " + _clip(inp["command"], 300))
                if bad:
                    errors.append(f"{p.get('name')}: {_clip(p.get('result') or '', 400)}")
            elif p.get("kind") == "text" and (p.get("text") or "").strip():
                if p.get("error"):
                    errors.append(_clip(p["text"], 400))
                else:
                    last_text = p["text"]
    if todos:
        marks = {"completed": "✅", "in_progress": "⏳"}
        out += ["", "## خطة العمل (آخر قائمة مهام)"]
        out += [f"- {marks.get(t.get('status'), '☐')} {t.get('content') or t.get('activeForm') or ''}" for t in todos
                if isinstance(t, dict)]
    if files:
        out += ["", "## الملفات التي عُدّلت أو أُنشئت"] + [f"- {v} {k}" for k, v in files.items()]
    if commands:
        out += ["", "## آخر الأوامر المنفّذة"] + [f"- {c}" for c in commands[-25:]]
    if errors:
        out += ["", "## آخر الأخطاء"] + [f"- {e}" for e in errors[-10:]]
    if last_text:
        out += ["", "## آخر ما كتبه المساعد", _clip(last_text, 3000)]
    return "\n".join(out) + "\n"


class StreamTranslator:
    """Turns Claude Code stream-json lines into the small event set the page understands,
    and collects the assistant message to store."""

    def __init__(self):
        self.deltas_in_message = False
        self.parts: List[Dict[str, Any]] = []
        self.session_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.model: Optional[str] = None

    def _text_part(self) -> Dict[str, Any]:
        if not self.parts or self.parts[-1]["kind"] != "text" or self.parts[-1].get("closed"):
            self.parts.append({"kind": "text", "text": ""})
        return self.parts[-1]

    def feed(self, evt: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        t = evt.get("type")
        if t == "system" and evt.get("subtype") == "init":
            self.session_id = evt.get("session_id") or self.session_id
            self.model = evt.get("model") or self.model
            out.append({"type": "init", "session_id": self.session_id, "model": self.model})
        elif t == "stream_event":
            if evt.get("parent_tool_use_id"):
                return out
            e = evt.get("event") or {}
            et = e.get("type")
            if et == "message_start":
                self.deltas_in_message = False
            elif et == "content_block_start" and (e.get("content_block") or {}).get("type") == "text":
                if self.parts and self.parts[-1]["kind"] == "text":
                    self.parts[-1]["closed"] = True
                self.parts.append({"kind": "text", "text": ""})
                out.append({"type": "segment"})
            elif et == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    self.deltas_in_message = True
                    self._text_part()["text"] += d["text"]
                    out.append({"type": "delta", "text": d["text"]})
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    out.append({"type": "thinking", "text": d["thinking"]})
        elif t == "assistant":
            if evt.get("parent_tool_use_id"):
                return out
            for block in (evt.get("message") or {}).get("content") or []:
                bt = block.get("type")
                if bt == "text" and not self.deltas_in_message and block.get("text"):
                    if self.parts and self.parts[-1]["kind"] == "text":
                        self.parts[-1]["closed"] = True
                    self.parts.append({"kind": "text", "text": block["text"], "closed": True})
                    out.append({"type": "text", "text": block["text"]})
                elif bt == "tool_use":
                    if self.parts and self.parts[-1]["kind"] == "text":
                        self.parts[-1]["closed"] = True
                    part = {"kind": "tool", "id": block.get("id"), "name": block.get("name"),
                            "input": block.get("input"), "result": None, "is_error": False}
                    self.parts.append(part)
                    out.append({"type": "tool_use", "id": part["id"], "name": part["name"], "input": part["input"]})
        elif t == "user":
            if evt.get("parent_tool_use_id"):
                return out
            content = (evt.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                text = _flatten(block.get("content"))[:6000]
                for part in self.parts:
                    if part["kind"] == "tool" and part["id"] == block.get("tool_use_id"):
                        part["result"], part["is_error"] = text, bool(block.get("is_error"))
                out.append({"type": "tool_result", "id": block.get("tool_use_id"), "content": text,
                            "is_error": bool(block.get("is_error"))})
        elif t == "result":
            self.session_id = evt.get("session_id") or self.session_id
            self.result = {
                "type": "result",
                "result": evt.get("result"),
                "is_error": bool(evt.get("is_error")),
                "cost_usd": evt.get("total_cost_usd"),
                "duration_ms": evt.get("duration_ms"),
                "num_turns": evt.get("num_turns"),
                "session_id": self.session_id,
                "subtype": evt.get("subtype"),
            }
            if self.result["is_error"] and not self.final_text() and evt.get("result"):
                self.parts.append({"kind": "text", "text": str(evt["result"]), "closed": True, "error": True})
            out.append(self.result)
        return out

    def final_text(self) -> str:
        return "\n\n".join(p["text"] for p in self.parts if p["kind"] == "text" and p["text"].strip())


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_flatten(c.get("text") if isinstance(c, dict) and "text" in c else
                                  (f"[{c.get('type')}]" if isinstance(c, dict) else c)) for c in content)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


# -------------------------------------------------------------------- server
class GuiApp:
    def __init__(self, ctx, project: Optional[Path] = None):
        self.ctx = ctx
        self.token = secrets.token_urlsafe(24)
        self.project = project or flows.resolve_project(ctx, None)
        self.lock = threading.Lock()
        self.runs: Dict[str, subprocess.Popen] = {}
        self.run_gateways: Dict[str, str] = {}
        self.run_emit: Dict[str, Any] = {}
        self.run_conv: Dict[str, int] = {}
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.conv_allowed: Dict[int, set] = {}
        self.preapproved: set = set()
        self.run_approvals: Dict[str, List[Dict[str, Any]]] = {}
        self.live: Dict[int, Dict[str, Any]] = {}
        self.resume_cancel: Dict[int, threading.Event] = {}
        self.approval_timeout = 1800.0
        self.httpd = None
        self.stopped: set = set()
        self.busy: set = set()
        self.started_gateways: set = set()
        self._local = threading.local()
        self.allowed_hosts: set = set()
        self.uploads = ctx.data_dir / "uploads"
        self.transcripts = ctx.data_dir / "conversations"
        self.reports = ctx.data_dir / "reports"
        self.projects_dir = ctx.data_dir / "projects"

    def store(self) -> Store:
        st = getattr(self._local, "store", None)
        if st is None:
            st = Store(self.ctx.data_dir / "allinone.db")
            self._local.store = st
        return st

    # ---------------------------------------------------------------- state
    def state(self) -> Dict[str, Any]:
        gws = []
        for gw in self.ctx.gateways().values():
            st = gw.status()
            gws.append({"name": gw.name, "title": gw.title, "type": gw.type_name, "installed": st["installed"],
                        "up": st["up"], "url": gw.base_url, "default_model": gw.default_model(),
                        "can_start": gw.type_name != "anthropic" and (gw.can_start() or gw.install_mode() != "none"),
                        "managed": bool(gw.managed_pid()) if gw.type_name != "anthropic" else False})
        claude = self.ctx.claude()
        last = self.ctx.state.get("last_gateway")
        names = [g["name"] for g in gws]
        gui_cfg = self.ctx.cfg.get("gui") or {}
        return {
            "version": __version__,
            "lang": i18n.language(),
            "gateways": gws,
            "default_gateway": last if last in names else (self.ctx.cfg.get("default_gateway") or (names[0] if names else "")),
            "project": str(self.project),
            "claude": {"installed": claude.is_installed(), "version": claude.installed_version()},
            "permission_modes": PERMISSION_MODES,
            "default_permission_mode": gui_cfg.get("permission_mode") or "acceptEdits",
            "efforts": EFFORTS,
            "deep_template": read_prompt(self.ctx, "deep_thinking.txt", ""),
            "max_upload_mb": self.max_upload() // (1024 * 1024),
            "report_ask": read_prompt(self.ctx, "report_request.txt", REPORT_ASK),
        }

    def max_upload(self) -> int:
        mb = (self.ctx.cfg.get("gui") or {}).get("max_upload_mb") or DEFAULT_UPLOAD_MB
        return int(float(mb) * 1024 * 1024)

    def stop_gateway(self, name: str) -> Dict[str, Any]:
        gw = self.ctx.gateway(name)
        with self.lock:
            busy = name in self.run_gateways.values()
        if busy:
            return {"ok": False, "error": tr("A reply is still running through {t}. Stop it first.",
                                             "ما زال هناك رد يعمل عبر {t}. أوقفه أولاً.", t=gw.title)}
        if gw.stop():
            self.started_gateways.discard(gw.name)
            return {"ok": True}
        if gw.is_up():
            return {"ok": False, "error": tr("{t} is running but was not started by Allinone; stop it where it was started.",
                                             "{t} يعمل لكنه لم يُشغَّل من Allinone؛ أوقفه من حيث شُغِّل.", t=gw.title)}
        return {"ok": True}

    # ------------------------------------------------------------------ chat
    def build_prompt(self, conv_id: int, text: str, deep: bool, memory: bool, attachments: List[str]) -> str:
        body = text
        if attachments:
            body += "\n\n" + tr("Attached files (read them before answering):", "الملفات المرفقة (اقرأها قبل الإجابة):")
            body += "".join(f"\n- {p}" for p in attachments)
        if deep:
            body = apply_deep(read_prompt(self.ctx, "deep_thinking.txt", DEEP_PLACEHOLDER), body)
        if memory:
            history = self.store().messages(conv_id)
            transcript = transcript_text(history)
            self.transcripts.mkdir(parents=True, exist_ok=True)
            path = self.transcripts / f"conversation-{conv_id}.txt"
            path.write_text(transcript + "\n", encoding="utf-8")
            if transcript.strip():
                tpl = read_prompt(self.ctx, "memory.txt", "{transcript}\n\n{message}")
                body = (tpl.replace("{file}", str(path)).replace("{transcript}", transcript)
                        .replace("{message}", body))
        return body

    def expert_prompt(self, answer: str, note: str) -> str:
        tpl = read_prompt(self.ctx, "expert_answer.txt", EXPERT_TEMPLATE)
        note_line = f"ملاحظة المستخدم: {note}" if note else ""  # same language as the template
        if "{answer}" not in tpl:
            tpl += "\n\n{answer}"
        return tpl.replace("{note}", note_line).replace("{answer}", answer).strip()

    def auto_resume_cfg(self) -> Dict[str, Any]:
        cfg = (self.ctx.cfg.get("gui") or {}).get("auto_resume")
        cfg = cfg if isinstance(cfg, dict) else {"enabled": bool(cfg)} if cfg is not None else {}
        return {"enabled": cfg.get("enabled", True), "max_attempts": int(cfg.get("max_attempts", 3)),
                "max_wait_minutes": float(cfg.get("max_wait_minutes", 360))}

    def handoff(self, conv_id: int, reason: str) -> Dict[str, str]:
        store = self.store()
        conv = store.conversation(conv_id)
        proj = store.project(conv["project_id"]) if conv is not None and conv["project_id"] else None
        text = build_handoff_text(dict(conv) if conv is not None else {}, store.messages(conv_id), reason,
                                  dict(proj) if proj is not None else None)
        self.transcripts.mkdir(parents=True, exist_ok=True)
        path = self.transcripts / f"handoff-{conv_id}.md"
        path.write_text(text, encoding="utf-8")
        return {"text": text, "path": str(path)}

    def handoff_prompt(self, conv_id: int, reason: str, message: str) -> str:
        ho = self.handoff(conv_id, reason)
        tpl = read_prompt(self.ctx, "handoff.txt", HANDOFF_TEMPLATE)
        return (tpl.replace("{reason}", reason).replace("{file}", ho["path"]).replace("{handoff}", ho["text"])
                .replace("{message}", message))

    def continue_prompt(self, reason: str) -> str:
        return read_prompt(self.ctx, "continue.txt", CONTINUE_TEMPLATE).replace("{reason}", reason)

    def last_interruption(self, conv_id: int) -> Optional[Dict[str, Any]]:
        for m in reversed(self.store().messages(conv_id)):
            if m["role"] == "assistant":
                return (m.get("meta") or {}).get("interrupted")
        return None

    def recover_interrupted(self) -> int:
        """Replies that were running when Allinone closed: keep what was saved and offer to continue."""
        count = 0
        for m in self.store().unfinished_messages():
            meta = m["meta"]
            meta["running"] = False
            meta["interrupted"] = diagnose(str(meta.get("gateway_type") or ""), "", closed=True)
            self.store().update_message(m["id"], m["display"], meta)
            count += 1
        return count

    def chat(self, req: Dict[str, Any], emit) -> None:
        text = str(req.get("text") or "").strip()
        expert = req.get("expert") if isinstance(req.get("expert"), dict) else None
        expert_answer = str((expert or {}).get("answer") or "").strip()
        resume_req = bool(req.get("resume"))
        if expert is not None:
            if not expert_answer:
                emit({"type": "error", "text": tr("The uploaded answer is empty.", "الإجابة المرفوعة فارغة.")})
                return
            text = text or tr("📥 External answer uploaded — apply it.", "📥 إجابة خارجية مرفوعة — طبّقها.")
        if resume_req:
            text = text or RESUME_LABEL
        if not text:
            emit({"type": "error", "text": tr("Empty message.", "الرسالة فارغة.")})
            return
        gw = self.ctx.gateway(req.get("gateway"))
        model = str(req.get("model") or "").strip() or None
        store = self.store()
        conv_id = int(req.get("conversation_id") or 0)
        conv = store.conversation(conv_id) if conv_id else None
        if resume_req and conv is None:
            emit({"type": "error", "text": tr("Nothing to continue.", "لا يوجد ما يُتابَع.")})
            return
        project_id = conv["project_id"] if conv is not None else (int(req.get("project_id") or 0) or None)
        proj = store.project(project_id) if project_id else None
        if proj is None:
            project_id = None
        folder = req.get("project") or (proj["folder"] if proj is not None and proj["folder"] else "") or self.project
        project = Path(str(folder)).expanduser()
        if not project.is_dir():
            emit({"type": "error", "text": tr("Project folder not found: {p}", "مجلد المشروع غير موجود: {p}", p=project)})
            return
        if conv is None:
            title = re.sub(r"\s+", " ", text)[:60]
            conv_id = store.new_conversation(title, gw.name, model or "", str(project), project_id=project_id)
            conv = store.conversation(conv_id)
            emit({"type": "conversation", "id": conv_id, "title": title, "project_id": project_id})
        if "preapprove" in req:
            self.set_preapprove(conv_id, bool(req.get("preapprove")))
        with self.lock:
            if conv_id in self.busy:
                emit({"type": "error", "text": tr("This conversation is still running.", "هذه المحادثة ما زالت قيد التنفيذ.")})
                return
            self.busy.add(conv_id)
            cancel = self.resume_cancel[conv_id] = threading.Event()
        write_lock = threading.Lock()
        raw_emit = emit

        def locked_emit(evt: Dict[str, Any]) -> None:
            with write_lock:  # approvals arrive from another thread
                raw_emit(evt)

        emit = locked_emit
        try:
            deep, memory = bool(req.get("deep")), bool(req.get("memory"))
            attachments = [str(a) for a in (req.get("attachments") or [])]
            switching = bool(conv["session_id"]) and conv["session_gateway"] != gw.name
            force_new = False
            if resume_req:
                last = self.last_interruption(conv_id) or {}
                reason = last.get("title") or tr("the previous run did not finish", "لم يكتمل التشغيل السابق")
                if switching or last.get("mode") == "handoff" or not conv["session_id"]:
                    body = self.handoff_prompt(conv_id, reason, self.continue_prompt(reason))
                    force_new = True
                else:
                    body = self.continue_prompt(reason)
                sent = body
            else:
                body = self.expert_prompt(expert_answer, text if expert and req.get("text") else "") if expert else text
                sent = self.build_prompt(conv_id, body, deep, memory, attachments)
                if switching and not memory and store.messages(conv_id):
                    # Another gateway cannot reuse the session: send a hand-over note instead of starting blind.
                    sent = self.handoff_prompt(conv_id, tr("switched gateway to {t}", "تغيير البوابة إلى {t}", t=gw.title), sent)
                    force_new = True
                    emit({"type": "notice", "text": tr(
                        "New gateway → new Claude Code session, with a hand-over note of the work so far.",
                        "بوابة مختلفة ← جلسة Claude Code جديدة، مع ملف تسليم يلخص العمل السابق.")})
            user_meta = {"deep": deep, "memory": memory, "attachments": attachments}
            if expert:
                user_meta.update(expert=True, expert_chars=len(expert_answer))
            if resume_req:
                user_meta["resume"] = "handoff" if force_new else "continue"
            store.add_message(conv_id, "user", text, sent, user_meta)

            auto = self.auto_resume_cfg()
            attempt = 0
            while True:
                diag = self._run_turn(conv_id, gw, model, project, project_id, sent, req, emit, force_new)
                if diag is None or not auto["enabled"] or not diag["retry"] or cancel.is_set():
                    break
                if attempt >= min(auto["max_attempts"], diag["attempts"] or auto["max_attempts"]):
                    emit({"type": "notice", "text": tr("Automatic resume gave up after {n} attempts.",
                                                       "توقفت المتابعة التلقائية بعد {n} محاولات.", n=attempt)})
                    break
                if diag["wait"] > auto["max_wait_minutes"] * 60:
                    break
                attempt += 1
                until = time.time() + diag["wait"]
                with self.lock:
                    self.live[conv_id] = {"waiting": {"until": until, "title": diag["title"], "attempt": attempt,
                                                     "max": auto["max_attempts"]}}
                emit({"type": "auto_resume", "wait": diag["wait"], "until": until, "attempt": attempt,
                      "max": auto["max_attempts"], "title": diag["title"], "reason": diag["reason"]})
                if cancel.wait(diag["wait"]):
                    emit({"type": "notice", "text": tr("Automatic resume cancelled.", "أُلغيت المتابعة التلقائية.")})
                    break
                force_new = diag["mode"] == "handoff"
                sent = (self.handoff_prompt(conv_id, diag["title"], self.continue_prompt(diag["title"])) if force_new
                        else self.continue_prompt(diag["title"]))
                label = AUTO_RESUME_LABEL.format(n=attempt, m=auto["max_attempts"])
                store.add_message(conv_id, "user", label, sent, {"resume": diag["mode"], "auto": True})
                emit({"type": "resume_start", "display": label, "sent": sent, "attempt": attempt})
        finally:
            with self.lock:
                self.busy.discard(conv_id)
                self.live.pop(conv_id, None)
                self.resume_cancel.pop(conv_id, None)

    def _run_turn(self, conv_id: int, gw, model: Optional[str], project: Path, project_id: Optional[int],
                  sent: str, req: Dict[str, Any], emit, force_new: bool = False) -> Optional[Dict[str, Any]]:
        """One Claude Code run. Saves the reply as it streams; returns None on success or a diagnosis."""
        store = self.store()
        conv = store.conversation(conv_id)
        run_id = secrets.token_hex(6)
        emit({"type": "start", "run_id": run_id, "conversation_id": conv_id, "sent": sent})
        base_meta = {"gateway": gw.name, "gateway_title": gw.title, "gateway_type": gw.type_name, "model": model or ""}
        resume = conv["session_id"] if (conv["session_id"] and conv["session_gateway"] == gw.name and not force_new) else None
        try:
            if flows.bring_up(self.ctx, gw, False):
                self.started_gateways.add(gw.name)
        except (GatewayError, ClaudeError, RuntimeError) as exc:
            diag = diagnose(gw.type_name, str(exc), gw_up=False)
            if gw.type_name == "anthropic" and diag["reason"] == "crash":
                diag = diagnose(gw.type_name, "not logged in " + str(exc))
            emit({"type": "error", "text": str(exc), "need_login": gw.type_name == "anthropic"})
            meta = {**base_meta, "error": True, "is_error": True, "interrupted": diag,
                    "parts": [{"kind": "text", "text": str(exc), "closed": True, "error": True}]}
            mid = store.add_message(conv_id, "assistant", str(exc), meta=meta)
            emit({"type": "saved", "message_id": mid, "meta": meta})
            return diag
        self.ctx.state.set("last_gateway", gw.name)

        gui_cfg = self.ctx.cfg.get("gui") or {}
        mode = str(req.get("permission_mode") or gui_cfg.get("permission_mode") or "acceptEdits")
        if mode not in PERMISSION_MODES:
            mode = "acceptEdits"
        effort = str(req.get("effort") or "")
        effort = effort if effort in EFFORTS else ""
        allowed = list((self.ctx.cfg.get("tasks") or {}).get("allowed_tools") or [])
        tr_ = StreamTranslator()
        # The reply is stored right away and updated while it streams, so nothing is lost if Allinone
        # or the computer stops in the middle.
        mid = store.add_message(conv_id, "assistant", "", meta={**base_meta, "running": True, "parts": tr_.parts})
        with self.lock:
            self.run_emit[run_id] = emit
            self.run_conv[run_id] = conv_id
            self.run_approvals[run_id] = []
            self.live[conv_id] = {"run_id": run_id, "tr": tr_, "message_id": mid}
        approval = None
        if mode != "bypassPermissions":
            approval = {"url": self.base_url() + "/internal/approval", "token": self.token, "run_id": run_id}
        err_tail: List[str] = []
        started = time.time()
        proc = None
        try:
            proc, session = self.ctx.claude().start_stream(gw, model, project, sent, resume=resume,
                                                           permission_mode=mode, allowed_tools=allowed,
                                                           effort=effort or None, approval=approval,
                                                           extra_args=self.run_extra_args(project_id))
            with self.lock:
                self.runs[run_id] = proc
                self.run_gateways[run_id] = gw.name

            def _drain_err():
                assert proc.stderr is not None
                for raw in proc.stderr:
                    err_tail.append(raw.decode("utf-8", "replace").rstrip())
                    del err_tail[:-40]

            threading.Thread(target=_drain_err, daemon=True).start()
            last_save = time.time()
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    for out in tr_.feed(evt):
                        emit(out)
                        if out["type"] == "init" and tr_.session_id:
                            # remember the session at once: a crash later can still be resumed from it
                            store.update_conversation(conv_id, session_id=tr_.session_id, session_gateway=gw.name,
                                                      gateway=gw.name, model=model or "")
                        if out["type"] in ("tool_result", "tool_use") or time.time() - last_save > 2:
                            store.update_message(mid, tr_.final_text(), {**base_meta, "running": True, "parts": tr_.parts})
                            last_save = time.time()
                proc.wait()
            finally:
                for pipe in (proc.stdout, proc.stderr):
                    try:
                        if pipe:
                            pipe.close()
                    except OSError:
                        pass
                session.cleanup()
        except (ClaudeError, OSError) as exc:
            err_tail.append(str(exc))
        finally:
            self._deny_pending(run_id)
            with self.lock:
                self.runs.pop(run_id, None)
                self.run_gateways.pop(run_id, None)
                self.run_emit.pop(run_id, None)
                self.run_conv.pop(run_id, None)
                approvals = self.run_approvals.pop(run_id, [])
                self.live.pop(conv_id, None)
                stopped = run_id in self.stopped
                self.stopped.discard(run_id)
        returncode = proc.returncode if proc is not None else -1
        if tr_.result is None:
            msg = tr("Stopped.", "تم الإيقاف.") if stopped else "\n".join(err_tail[-15:]) or tr(
                "Claude Code exited with code {c}.", "خرج Claude Code بالرمز {c}.", c=returncode)
            if not stopped:
                emit({"type": "error", "text": msg, "need_login": "login" in msg.lower()})
            tr_.result = {"type": "result", "is_error": not stopped, "result": msg,
                          "duration_ms": int((time.time() - started) * 1000), "stopped": stopped}
            if not tr_.final_text():
                tr_.parts.append({"kind": "text", "text": msg, "closed": True, "error": not stopped})
        if tr_.session_id:
            store.update_conversation(conv_id, session_id=tr_.session_id, session_gateway=gw.name,
                                      gateway=gw.name, model=model or "")
        diag = None
        if stopped or tr_.result.get("is_error"):
            detail = "\n".join([str(tr_.result.get("result") or ""), *err_tail[-15:]])
            diag = diagnose(gw.type_name, detail, gw_up=gw.type_name == "anthropic" or gw.is_up(), stopped=stopped,
                            subtype=str(tr_.result.get("subtype") or ""))
            if diag["reason"] == "session_missing":
                store.update_conversation(conv_id, session_id=None, session_gateway=None)
        meta = {**base_meta, "parts": tr_.parts, "running": False,
                "model": model or tr_.model or gw.default_model(), "stopped": stopped, "approvals": approvals,
                **{k: tr_.result.get(k) for k in ("cost_usd", "duration_ms", "num_turns", "is_error")}}
        if diag:
            meta["interrupted"] = diag
        store.update_message(mid, tr_.final_text(), meta)
        emit({"type": "saved", "message_id": mid, "meta": meta})
        return diag

    def live_state(self, conv_id: int) -> Dict[str, Any]:
        """For a page that (re)opens a conversation while its reply is still running."""
        with self.lock:
            live = dict(self.live.get(conv_id) or {})
            running = conv_id in self.busy
            run_id = live.get("run_id")
            pending = [{"id": a, "tool": i["tool"], "input": i["input"]} for a, i in self.pending.items()
                       if i["run_id"] == run_id] if run_id else []
        tr_ = live.get("tr")
        return {"running": running, "run_id": run_id, "parts": json.loads(json.dumps(tr_.parts)) if tr_ else [],
                "pending": pending, "waiting": live.get("waiting")}

    def cancel_resume(self, conv_id: int) -> bool:
        with self.lock:
            ev = self.resume_cancel.get(conv_id)
        if ev is not None:
            ev.set()
        return ev is not None

    # ------------------------------------------------------------- approvals
    def base_url(self) -> str:
        host = next((h for h in sorted(self.allowed_hosts) if h.startswith("127.0.0.1")), "127.0.0.1:8787")
        return f"http://{host}"

    def request_approval(self, run_id: str, tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Called (blocking) by approval_server.py; waits for the person to click Allow or Deny."""
        with self.lock:
            emit = self.run_emit.get(run_id)
            conv = self.run_conv.get(run_id)
            if conv is not None and tool_name in self.conv_allowed.get(conv, set()):
                return {"behavior": "allow", "updatedInput": tool_input}
            auto = conv is not None and conv in self.preapproved
            if emit is not None:
                self.run_approvals.setdefault(run_id, []).append({"tool": tool_name, "behavior": "allow" if auto else "",
                                                                  "auto": auto})
            if emit is not None and auto:
                emit({"type": "approval_auto", "tool": tool_name, "input": tool_input})
                return {"behavior": "allow", "updatedInput": tool_input}
            if emit is None:
                return {"behavior": "deny", "message": "This run is no longer attached to the GUI."}
            aid = secrets.token_hex(5)
            item = {"id": aid, "run_id": run_id, "tool": tool_name, "input": tool_input,
                    "event": threading.Event(), "decision": None}
            self.pending[aid] = item
        emit({"type": "approval", "id": aid, "tool": tool_name, "input": tool_input})
        answered = item["event"].wait(self.approval_timeout)
        with self.lock:
            self.pending.pop(aid, None)
        decision = item["decision"] if answered and item["decision"] else {
            "behavior": "deny", "message": tr("No answer from the user in time.", "لم يُجب المستخدم في الوقت المحدد.")}
        emit({"type": "approval_done", "id": aid, "behavior": decision["behavior"]})
        with self.lock:
            for rec in reversed(self.run_approvals.get(run_id, [])):
                if rec["tool"] == tool_name and not rec["behavior"]:
                    rec["behavior"] = decision["behavior"]
                    break
        return decision

    def set_preapprove(self, conv_id: int, on: bool) -> int:
        """Pre-approve (or stop pre-approving) Claude Code permission requests in one conversation.
        Turning it on also answers the requests already waiting in that conversation."""
        with self.lock:
            if on:
                self.preapproved.add(conv_id)
            else:
                self.preapproved.discard(conv_id)
            waiting = [a for a, i in self.pending.items() if on and self.run_conv.get(i["run_id"]) == conv_id]
        for aid in waiting:
            self.answer_approval(aid, True)
        return len(waiting)

    def answer_approval(self, aid: str, allow: bool, scope: str = "once", message: str = "") -> bool:
        with self.lock:
            item = self.pending.get(aid)
            if item is None:
                return False
            if allow:
                item["decision"] = {"behavior": "allow", "updatedInput": item["input"]}
                conv = self.run_conv.get(item["run_id"])
                if scope == "conversation" and conv is not None:
                    self.conv_allowed.setdefault(conv, set()).add(item["tool"])
            else:
                item["decision"] = {"behavior": "deny",
                                    "message": message or tr("The user denied this action.", "رفض المستخدم هذا الإجراء.")}
            item["event"].set()
        return True

    def _deny_pending(self, run_id: str) -> None:
        with self.lock:
            items = [i for i in self.pending.values() if i["run_id"] == run_id]
        for item in items:
            item["decision"] = {"behavior": "deny", "message": "Stopped."}
            item["event"].set()

    def stop_run(self, run_id: str) -> bool:
        with self.lock:
            proc = self.runs.get(run_id)
            conv = self.run_conv.get(run_id)
            if conv is not None and conv in self.resume_cancel:
                self.resume_cancel[conv].set()  # a stopped reply must not resume by itself
            if proc is None:
                return False
            self.stopped.add(run_id)
        self._deny_pending(run_id)
        procs.kill_tree(proc.pid, timeout=5)
        return True

    # ----------------------------------------------------------------- login
    def login(self) -> Dict[str, Any]:
        claude = self.ctx.claude()
        gw = next((g for g in self.ctx.gateways().values() if g.type_name == "anthropic"), None)
        cmd = claude.auth_command("login")
        env = claude.base_env(gw)
        if os.name == "nt":
            subprocess.Popen(cmd, env=env, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
            return {"ok": True, "mode": "window"}
        line = " ".join(shlex.quote(c) for c in cmd)
        script = f"export CLAUDE_CONFIG_DIR={shlex.quote(env.get('CLAUDE_CONFIG_DIR', ''))}; {line}"
        if sys.platform == "darwin":
            apple = script.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{apple}"',
                              "-e", 'tell application "Terminal" to activate'])
            return {"ok": True, "mode": "window"}
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            exe = procs.which(term)
            if exe:
                args = [exe, "--", "sh", "-c", script + "; exec sh"] if term == "gnome-terminal" else \
                    [exe, "-e", "sh", "-c", script + "; exec sh"]
                subprocess.Popen(args)
                return {"ok": True, "mode": "window"}
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "mode": "background"}

    def logout(self) -> Dict[str, Any]:
        claude = self.ctx.claude()
        gw = next((g for g in self.ctx.gateways().values() if g.type_name == "anthropic"), None)
        res = subprocess.run(claude.auth_command("logout"), env=claude.base_env(gw), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=60)
        return {"ok": res.returncode == 0, "text": res.stdout.decode("utf-8", "replace")[-500:]}

    # ------------------------------------------------------------- utilities
    def upload_path(self, name: str) -> Path:
        safe = re.sub(r"[^\w.\-]+", "_", Path(name.replace("\\", "/")).name, flags=re.UNICODE).strip("._")[:80] or "file"
        folder = self.uploads / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{time.strftime('%H%M%S')}-{secrets.token_hex(2)}-{safe}"

    def pick_folder(self, initial: str) -> Optional[str]:
        try:
            import tkinter
            from tkinter import filedialog
        except Exception:
            return None
        result: List[str] = []

        def _run():
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(initialdir=initial or str(Path.home()))
            root.destroy()
            if chosen:
                result.append(chosen)

        t = threading.Thread(target=_run)
        t.start()
        t.join(600)
        return result[0] if result else ""

    def build_report(self, conv_id: int) -> Dict[str, Any]:
        store = self.store()
        conv = store.conversation(conv_id)
        if conv is None:
            raise ValueError(tr("Conversation not found.", "المحادثة غير موجودة."))
        proj = store.project(conv["project_id"]) if conv["project_id"] else None
        text = build_report_text(dict(conv), store.messages(conv_id), read_prompt(self.ctx, "report_header.txt", REPORT_HEADER),
                                 dict(proj) if proj is not None else None)
        self.reports.mkdir(parents=True, exist_ok=True)
        path = self.reports / f"report-{conv_id}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "name": path.name, "text": text, "chars": len(text)}

    def project_files_dir(self, project_id: int) -> Path:
        return self.projects_dir / str(project_id) / "files"

    def project_payload(self, project_id: int) -> Optional[Dict[str, Any]]:
        store = self.store()
        proj = store.project(project_id)
        if proj is None:
            return None
        data = dict(proj)
        data["files"] = [dict(r) for r in store.project_files(project_id)]
        data["conversations"] = [dict(r) for r in store.conversations(project_id=project_id)]
        return data

    def run_extra_args(self, project_id: Optional[int]) -> List[str]:
        """Folders Claude Code may read besides the working folder, plus the project's instructions."""
        dirs = [self.uploads, self.reports, self.transcripts]
        args: List[str] = []
        proj = self.store().project(project_id) if project_id else None
        if proj is not None:
            files = self.store().project_files(project_id)
            dirs.append(self.project_files_dir(project_id))
            lines = [f"# مشروع: {proj['name']}"]
            if proj["description"]:
                lines += ["", proj["description"]]
            if proj["instructions"].strip():
                lines += ["", "## تعليمات المشروع (اتبعها في كل رد)", proj["instructions"].strip()]
            if files:
                lines += ["", "## ملفات معرفة المشروع (اقرأ منها ما يلزم قبل الإجابة)"]
                lines += [f"- {f['name']}: {f['path']}" for f in files]
            prompt_file = self.projects_dir / str(project_id) / "system-prompt.md"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            args += ["--append-system-prompt-file", str(prompt_file)]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            args += ["--add-dir", str(d)]
        return args

    def save_project(self, project_id: Optional[int], body: Dict[str, Any]) -> int:
        fields = {k: body[k] for k in self.store().PROJECT_FIELDS if k in body}
        if fields.get("folder"):
            folder = Path(str(fields["folder"])).expanduser()
            if not folder.is_dir():
                raise ValueError(tr("Folder not found: {p}", "المجلد غير موجود: {p}", p=folder))
            fields["folder"] = str(folder.resolve())
        if "name" in fields:
            fields["name"] = str(fields["name"] or "").strip()[:120] or tr("Project", "مشروع")
        if "instructions" in fields:
            fields["instructions"] = str(fields["instructions"] or "")[:100000]
        if project_id:
            if self.store().project(project_id) is None:
                raise ValueError(tr("Project not found.", "المشروع غير موجود."))
            self.store().update_project(project_id, **fields)
            return project_id
        return self.store().new_project(**fields)

    def delete_project(self, project_id: int) -> None:
        self.store().delete_project(project_id)
        import shutil
        shutil.rmtree(self.projects_dir / str(project_id), ignore_errors=True)

    def export_markdown(self, conv_id: int) -> str:
        conv = self.store().conversation(conv_id)
        lines = [f"# {conv['title'] if conv else conv_id}", ""]
        for m in self.store().messages(conv_id):
            meta = m.get("meta") or {}
            who = "**أنت**" if m["role"] == "user" else f"**Claude** ({meta.get('gateway_title') or meta.get('gateway') or ''}" \
                                                         f"{' · ' + meta['model'] if meta.get('model') else ''})"
            lines += [who, "", m["display"], ""]
        return "\n".join(lines)

    def shutdown(self) -> None:
        for run_id in list(self.runs):
            self.stop_run(run_id)
        if not self.ctx.cfg.get("keep_gateway_running", False):
            for name in self.started_gateways:
                gw = self.ctx.gateway(name)
                if gw.managed_pid():
                    ui.step(tr("Stopping {t}", "إيقاف {t}", t=gw.title))
                    gw.stop()


def make_handler(app: GuiApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Allinone"

        def log_message(self, *args):
            pass

        # ------------------------------------------------------------ helpers
        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in app.allowed_hosts

        def _token_ok(self, query: Dict[str, List[str]]) -> bool:
            given = self.headers.get("X-Allinone-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(given, app.token)

        def _json(self, code: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _discard(self, length: int) -> None:
            """Read and drop a request body, so the browser gets our reply instead of a reset connection."""
            while length > 0:
                chunk = self.rfile.read(min(1 << 20, length))
                if not chunk:
                    break
                length -= len(chunk)

        def _upload(self, query: Dict[str, List[str]], project_id: Optional[int] = None) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            name = (query.get("name") or ["file"])[0]
            limit = app.max_upload()
            if length > limit:
                self._discard(length)
                return self._json(413, {"error": tr("{n} is too large ({s} MB, limit {l} MB).",
                                                    "الملف {n} كبير جداً ({s} MB، والحد {l} MB).", n=name,
                                                    s=length // (1024 * 1024), l=limit // (1024 * 1024))})
            if project_id is not None:
                if app.store().project(project_id) is None:
                    self._discard(length)
                    return self._json(404, {"error": tr("Project not found.", "المشروع غير موجود.")})
                folder = app.project_files_dir(project_id)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / app.upload_path(name).name
            else:
                path = app.upload_path(name)
            remaining = length
            with open(path, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                path.unlink()
                return self._json(400, {"error": tr("Upload of {n} was interrupted.", "انقطع رفع الملف {n}.", n=name)})
            if project_id is not None:
                fid = app.store().add_project_file(project_id, Path(name.replace("\\", "/")).name[:120] or "file",
                                                   str(path), length)
                return self._json(200, {"id": fid, "path": str(path), "size": length})
            self._json(200, {"path": str(path), "size": length})

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_JSON:
                self._discard(length)
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}

        def _guard(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return None
            if parts.path.startswith(("/api/", "/internal/")) and not self._token_ok(query):
                self._json(401, {"error": "bad token"})
                return None
            return parts.path, query

        # ------------------------------------------------------------- routes
        def do_GET(self):
            g = self._guard()
            if g is None:
                return
            path, query = g
            try:
                if path in ("/", "/index.html"):
                    data = (WEB_DIR / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
                                     "'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/api/state":
                    self._json(200, app.state())
                elif path == "/api/models":
                    gw = app.ctx.gateway((query.get("gateway") or [None])[0])
                    self._json(200, {"models": gw.models() if gw.is_up() else None})
                elif path == "/api/auth":
                    self._json(200, app.ctx.claude().auth_status())
                elif path == "/api/conversations":
                    pid = (query.get("project") or [""])[0]
                    rows = app.store().conversations(query=(query.get("q") or [""])[0],
                                                     project_id=int(pid) if pid.isdigit() else None)
                    self._json(200, {"conversations": [{**dict(r), "running": r["id"] in app.busy} for r in rows]})
                elif m := re.fullmatch(r"/api/conversations/(\d+)", path):
                    cid = int(m.group(1))
                    conv = app.store().conversation(cid)
                    if not conv:
                        return self._json(404, {"error": "not found"})
                    data = dict(conv)
                    data["preapproved"] = cid in app.preapproved
                    data["running"] = cid in app.busy
                    proj = app.store().project(conv["project_id"]) if conv["project_id"] else None
                    data["project_name"] = proj["name"] if proj is not None else ""
                    self._json(200, {"conversation": data, "messages": app.store().messages(cid)})
                elif m := re.fullmatch(r"/api/conversations/(\d+)/live", path):
                    self._json(200, app.live_state(int(m.group(1))))
                elif m := re.fullmatch(r"/api/conversations/(\d+)/handoff", path):
                    self._json(200, app.handoff(int(m.group(1)), tr("manual hand-over", "تسليم يدوي")))
                elif path == "/api/projects":
                    self._json(200, {"projects": app.store().projects()})
                elif m := re.fullmatch(r"/api/projects/(\d+)", path):
                    data = app.project_payload(int(m.group(1)))
                    if data is None:
                        return self._json(404, {"error": "not found"})
                    self._json(200, {"project": data})
                elif m := re.fullmatch(r"/api/conversations/(\d+)/export", path):
                    data = app.export_markdown(int(m.group(1))).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="conversation-{m.group(1)}.md"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                else:
                    self._json(404, {"error": "not found"})
            except (GatewayError, ClaudeError, ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})

        def do_DELETE(self):
            g = self._guard()
            if g is None:
                return
            if m := re.fullmatch(r"/api/conversations/(\d+)", g[0]):
                app.store().delete_conversation(int(m.group(1)))
                return self._json(200, {"ok": True})
            if m := re.fullmatch(r"/api/projects/(\d+)", g[0]):
                app.delete_project(int(m.group(1)))
                return self._json(200, {"ok": True})
            if m := re.fullmatch(r"/api/projects/(\d+)/files/(\d+)", g[0]):
                rows = {r["id"]: r for r in app.store().project_files(int(m.group(1)))}
                if int(m.group(2)) not in rows:
                    return self._json(404, {"error": "not found"})
                path = app.store().remove_project_file(int(m.group(2)))
                try:
                    if path:
                        Path(path).unlink()
                except OSError:
                    pass
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            g = self._guard()
            if g is None:
                return
            path, query = g
            try:
                if path == "/api/upload":
                    return self._upload(query)
                if m := re.fullmatch(r"/api/projects/(\d+)/files", path):
                    return self._upload(query, project_id=int(m.group(1)))
                body = self._body()
                if path == "/internal/approval":
                    return self._json(200, app.request_approval(str(body.get("run_id") or ""),
                                                                str(body.get("tool_name") or ""),
                                                                body.get("input") if isinstance(body.get("input"), dict) else {}))
                if path == "/api/approval":
                    ok = app.answer_approval(str(body.get("id") or ""), bool(body.get("allow")),
                                             str(body.get("scope") or "once"), str(body.get("message") or ""))
                    return self._json(200, {"ok": ok})
                if path == "/api/chat":
                    return self._chat(body)
                if path == "/api/stop":
                    cancelled = app.cancel_resume(int(body.get("conversation_id") or 0))
                    return self._json(200, {"ok": app.stop_run(str(body.get("run_id") or "")) or cancelled})
                if path == "/api/gateway":
                    gw = app.ctx.gateway(body.get("name"))
                    if body.get("action") == "stop":
                        return self._json(200, app.stop_gateway(gw.name))
                    flows.ensure_installed(app.ctx, gw, False)
                    if gw.start() == "started":
                        app.started_gateways.add(gw.name)
                    return self._json(200, {"ok": True, "notes": gw.dashboard_notes()})
                if path == "/api/dashboard":
                    gw = app.ctx.gateway(body.get("name"))
                    if gw.type_name != "anthropic":
                        flows.ensure_installed(app.ctx, gw, False)
                        if gw.start() == "started":
                            app.started_gateways.add(gw.name)
                    return self._json(200, {"url": gw.dashboard_url(), "notes": gw.dashboard_notes()})
                if path == "/api/login":
                    return self._json(200, app.login())
                if path == "/api/logout":
                    return self._json(200, app.logout())
                if path == "/api/shutdown":
                    self._json(200, {"ok": True})
                    if app.httpd is not None:
                        threading.Thread(target=app.httpd.shutdown, daemon=True).start()
                    return None
                if path == "/api/memory":
                    mid = app.store().remember(str(body.get("text") or "")[:20000], tags=str(body.get("tags") or ""),
                                               source="gui")
                    return self._json(200, {"id": mid})
                if path == "/api/project":
                    raw = str(body.get("path") or "")
                    if body.get("browse"):
                        picked = app.pick_folder(raw)
                        if picked is None:
                            return self._json(400, {"error": tr("Folder picker unavailable — type the path.",
                                                                 "اختيار المجلد غير متاح — اكتب المسار.")})
                        if not picked:
                            return self._json(200, {"path": raw})
                        raw = picked
                    p = Path(raw).expanduser()
                    if not p.is_dir():
                        return self._json(400, {"error": tr("Folder not found: {p}", "المجلد غير موجود: {p}", p=p)})
                    return self._json(200, {"path": str(p.resolve())})
                if m := re.fullmatch(r"/api/conversations/(\d+)/report", path):
                    return self._json(200, app.build_report(int(m.group(1))))
                if path == "/api/preapprove":
                    cid = int(body.get("conversation_id") or 0)
                    if not cid:
                        return self._json(400, {"error": "conversation_id"})
                    return self._json(200, {"ok": True, "answered": app.set_preapprove(cid, bool(body.get("on")))})
                if path == "/api/projects":
                    pid = app.save_project(None, body)
                    return self._json(200, {"project": app.project_payload(pid)})
                if m := re.fullmatch(r"/api/projects/(\d+)", path):
                    pid = app.save_project(int(m.group(1)), body)
                    return self._json(200, {"project": app.project_payload(pid)})
                if m := re.fullmatch(r"/api/conversations/(\d+)/rename", path):
                    app.store().update_conversation(int(m.group(1)), title=str(body.get("title") or "")[:120])
                    return self._json(200, {"ok": True})
                if path == "/api/lang":
                    code = str(body.get("lang") or "")
                    if code in i18n.SUPPORTED:
                        app.ctx.state.set("language", code)
                        i18n.set_language(code)
                    return self._json(200, {"lang": i18n.language()})
                self._json(404, {"error": "not found"})
            except (GatewayError, ClaudeError, ValueError, OSError, procs.CommandError) as exc:
                self._json(400, {"error": str(exc)})

        def _chat(self, body: Dict[str, Any]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            alive = [True]

            def emit(evt: Dict[str, Any]) -> None:
                if not alive[0]:
                    return  # the page went away; keep running so the answer is still saved
                try:
                    self.wfile.write((json.dumps(evt, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except OSError:
                    alive[0] = False

            try:
                app.chat(body, emit)
            except (GatewayError, ClaudeError, ValueError, OSError) as exc:
                emit({"type": "error", "text": str(exc)})
            emit({"type": "end"})
            self.close_connection = True

    return Handler


def serve(ctx, port: int = 0, open_browser: bool = True, project: Optional[Path] = None) -> int:
    app = GuiApp(ctx, project)
    recovered = app.recover_interrupted()
    if recovered:
        ui.warn(tr("{n} reply(ies) were interrupted last time; open them and press Continue.",
                   "{n} رد انقطع في المرة السابقة؛ افتحه واضغط «متابعة».", n=recovered))
    gui_cfg = ctx.cfg.get("gui") or {}
    wanted = int(port or gui_cfg.get("port") or 8787)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", wanted), make_handler(app))
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    real_port = httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    app.httpd = httpd
    url = f"http://127.0.0.1:{real_port}/#t={app.token}"
    ui.ok(tr("Allinone GUI is running: {u}", "واجهة Allinone تعمل على: {u}", u=url))
    ui.say(ui.dim(tr("Keep this window open. Press Ctrl+C to quit.", "أبقِ هذه النافذة مفتوحة. اضغط Ctrl+C للخروج.")))
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    url_file = ctx.run_dir / "gui-url.txt"
    try:
        url_file.write_text(url, encoding="utf-8")
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        app.shutdown()
        try:
            url_file.unlink()
        except OSError:
            pass
    return 0
