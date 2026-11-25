"""Claude.ai by hand: work with any Claude account in the browser (free ones too), with the project local.

Allinone builds one self-contained request (how to answer, the work so far, the
file tree, the files that matter, the last check results, your request). You
paste it into claude.ai — any account — and paste the answer back. Allinone
shows which files the answer creates, changes or deletes, writes the ones you
accept into your folder (keeping a backup for undo), and can run the commands it
suggests and the project's check. Nothing is kept on Claude's side: the next
request is built again from your files, so you can continue from another
account at any time.

The answer format (explained to Claude in config/prompts/bridge_request.txt):
    <<<FILE: path/to/file.ext>>>   …the whole file…   <<<END FILE>>>
    <<<DELETE: path/to/old.ext>>>
    <<<RUN: npm install>>>
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next", ".nuxt",
             ".allinone-visual", ".idea", ".vscode", "coverage", ".cache", ".pytest_cache", ".mypy_cache", "target",
             "bin", "obj", ".gradle", ".dart_tool", "Pods", ".turbo", ".parcel-cache", ".svelte-kit"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "composer.lock",
              ".DS_Store", "Thumbs.db"}
MAX_FILE = 120_000

REQUEST_TEMPLATE = """أنت مبرمج خبير تعمل على مشروع موجود على جهاز المستخدم. لا ترى الملفات مباشرة؛ أُرسلها لك هنا.
سيُنسخ ردك ويُطبَّق آلياً على جهاز المستخدم بواسطة أداة Allinone، لذلك التزم بصيغة الرد بدقة.

===== كيف تكتب ردك (مهم جداً) =====
- لا تستخدم Artifacts ولا ملفات مرفقة: اكتب كل شيء داخل نص الرد نفسه.
- لكل ملف تنشئه أو تعدّله اكتب الملف كاملاً، وليس مقتطفاً أو «بقية الكود كما هو»، بين العلامتين:
<<<FILE: المسار/النسبي/للملف.ext>>>
(محتوى الملف كاملاً)
<<<END FILE>>>
- لحذف ملف: <<<DELETE: المسار/النسبي>>>
- لأمر يجب تشغيله في مجلد المشروع (تثبيت، اختبار، تشغيل): <<<RUN: الأمر>>>
- المسارات نسبية إلى مجلد المشروع، ولا تكتب ملفات خارجه.
- اكتب شرحك المختصر خارج هذه العلامات. إذا كانت هناك معلومة ناقصة تغيّر الحل فاسأل سؤالاً واحداً محدداً.
- قبل أن تكتب الحل تأكد أنه يعمل مع بقية الملفات المرسلة، واذكر في النهاية كيف أتحقق أنه يعمل.

===== المشروع =====
{project}
{instructions}
===== ما أُنجز حتى الآن (ملف تسليم يُكتب محلياً) =====
{handoff}

===== شجرة ملفات المشروع =====
{tree}

===== محتوى الملفات المهمة =====
{files}
{checks}
===== المطلوب الآن =====
{request}
"""


def _rel_ok(folder: Path, rel: str) -> Optional[Path]:
    """A path inside the project folder, or None."""
    rel = rel.strip().strip("`'\"").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    target = (folder / rel).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return None
    if any(part in (".git",) for part in Path(rel).parts):
        return None
    return target


def is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def walk(folder: Path, limit: int = 2000) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".allinone"))
        for f in sorted(files):
            if f in SKIP_FILES:
                continue
            p = Path(root) / f
            try:
                out.append((p.relative_to(folder).as_posix(), p.stat().st_size))
            except OSError:
                continue
            if len(out) >= limit:
                return out
    return out


def tree_text(entries: List[Tuple[str, int]], limit: int = 400) -> str:
    lines = [f"{rel}  ({size:,} بايت)" for rel, size in entries[:limit]]
    if len(entries) > limit:
        lines.append(f"… و{len(entries) - limit} ملفاً آخر")
    return "\n".join(lines) or "(المجلد فارغ)"


def pick_files(folder: Path, message: str, attachments: List[str], recent: List[str], budget: int) -> Dict[str, Any]:
    """Which file contents to send, in order of importance, within the size budget."""
    entries = walk(folder)
    by_rel = {rel: size for rel, size in entries}
    chosen: List[str] = []

    def want(rel: str) -> None:
        if rel in by_rel and rel not in chosen:
            chosen.append(rel)

    low = message.lower()
    for rel in by_rel:  # files named in the request
        name = rel.rsplit("/", 1)[-1].lower()
        if rel.lower() in low or (len(name) > 3 and name in low):
            want(rel)
    for rel in recent:  # files the earlier answers wrote
        want(rel)
    small_first = sorted((r for r in by_rel if r not in chosen), key=lambda r: by_rel[r])
    for rel in small_first:  # then the rest, small files first, while there is room
        want(rel)
    files, skipped, used = [], [], 0
    for rel in chosen:
        p = folder / rel
        size = by_rel[rel]
        if size > MAX_FILE or rel.endswith((".min.js", ".min.css", ".map")) or not is_text(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > budget:
            skipped.append(rel)
            continue
        files.append((rel, text))
        used += len(text)
    for att in attachments:  # attached files from outside the project
        ap = Path(att)
        if ap.is_file() and ap.stat().st_size <= MAX_FILE and is_text(ap):
            text = ap.read_text(encoding="utf-8", errors="replace")
            if used + len(text) <= budget:
                files.append((f"(مرفق) {ap.name}", text))
                used += len(text)
    return {"files": files, "skipped": skipped, "entries": entries, "chars": used}


def files_text(files: List[Tuple[str, str]], skipped: List[str]) -> str:
    parts = [f"<<<FILE: {rel}>>>\n{text.rstrip()}\n<<<END FILE>>>" for rel, text in files]
    if skipped:
        parts.append("ملفات لم تُرسل لضيق المساحة (اطلبها بالاسم إن احتجتها): " + "، ".join(skipped[:60]))
    return "\n\n".join(parts) or "(لا توجد ملفات نصية بعد)"


# ------------------------------------------------------------------ the answer
_FILE_RE = re.compile(r"<<<\s*FILE\s*:\s*(.+?)\s*>>>\s*\n(.*?)\n?\s*<<<\s*END\s*FILE\s*>>>", re.S | re.I)
_DEL_RE = re.compile(r"<<<\s*DELETE\s*:\s*(.+?)\s*>>>", re.I)
_RUN_RE = re.compile(r"<<<\s*RUN\s*:\s*(.+?)\s*>>>", re.I)
_FENCE_HEAD = re.compile(r"^(?:#{1,6}\s*|\*\*|`)?\s*(?:File|ملف|الملف)?\s*:?\s*`?([\w.\-/\\]+\.[\w]+)`?\s*(?:\*\*)?\s*:?\s*$", re.I)


def _strip_fence(content: str) -> str:
    m = re.match(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", content, re.S)
    return m.group(1) if m else content


def parse_answer(text: str) -> Dict[str, Any]:
    files: Dict[str, str] = {}
    for path, content in _FILE_RE.findall(text):
        files[path.strip().strip("`")] = _strip_fence(content)
    rest = _FILE_RE.sub("", text)
    if not files:  # fallback: a file name line followed by a fenced block
        lines = text.splitlines()
        i = 0
        while i < len(lines) - 1:
            head = _FENCE_HEAD.match(lines[i].strip())
            if head and lines[i + 1].strip().startswith("```"):
                j = i + 2
                while j < len(lines) and not lines[j].strip().startswith("```"):
                    j += 1
                files[head.group(1)] = "\n".join(lines[i + 2:j])
                i = j + 1
                continue
            i += 1
    deletes = [d.strip().strip("`") for d in _DEL_RE.findall(text)]
    runs = [r.strip().strip("`") for r in _RUN_RE.findall(text)]
    rest = _RUN_RE.sub("", _DEL_RE.sub("", rest)).strip()
    return {"files": files, "deletes": deletes, "runs": runs, "text": rest}


def plan(folder: Path, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for rel, content in parsed["files"].items():
        target = _rel_ok(folder, rel)
        if target is None:
            out.append({"path": rel, "action": "rejected", "why": "خارج مجلد المشروع"})
            continue
        old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
        if old is not None and old.rstrip("\n") == content.rstrip("\n"):
            action = "same"
        else:
            action = "modify" if old is not None else "create"
        diff = list(difflib.unified_diff((old or "").splitlines(), content.splitlines(), lineterm="", n=0))
        out.append({"path": rel, "action": action, "size": len(content.encode("utf-8")),
                    "added": sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
                    "removed": sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))})
    for rel in parsed["deletes"]:
        target = _rel_ok(folder, rel)
        out.append({"path": rel, "action": "delete" if target is not None and target.is_file() else "rejected",
                    "why": "" if target is not None and target.is_file() else "غير موجود أو خارج المجلد"})
    return out


def apply(folder: Path, parsed: Dict[str, Any], paths: List[str], backup: Path) -> List[Dict[str, Any]]:
    """Write the accepted files; the previous versions go to `backup` (manifest.json) for undo."""
    backup.mkdir(parents=True, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    done = []
    wanted = set(paths)
    for rel, content in parsed["files"].items():
        target = _rel_ok(folder, rel)
        if rel not in wanted or target is None:
            continue
        entry = {"path": rel, "existed": target.is_file()}
        if target.is_file():
            saved = backup / f"{len(manifest):03d}.bak"
            shutil.copy2(target, saved)
            entry["backup"] = saved.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        manifest.append(entry)
        done.append({"path": rel, "action": "modify" if entry["existed"] else "create"})
    for rel in parsed["deletes"]:
        target = _rel_ok(folder, rel)
        if rel not in wanted or target is None or not target.is_file():
            continue
        saved = backup / f"{len(manifest):03d}.bak"
        shutil.copy2(target, saved)
        target.unlink()
        manifest.append({"path": rel, "existed": True, "backup": saved.name, "deleted": True})
        done.append({"path": rel, "action": "delete"})
    (backup / "manifest.json").write_text(json.dumps({"folder": str(folder), "time": time.time(), "files": manifest},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return done


def undo(backup: Path) -> List[str]:
    data = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    folder = Path(data["folder"])
    restored = []
    for entry in reversed(data["files"]):
        target = _rel_ok(folder, entry["path"])
        if target is None:
            continue
        if entry.get("backup"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / entry["backup"], target)
        elif target.is_file():
            target.unlink()
        restored.append(entry["path"])
    return restored
