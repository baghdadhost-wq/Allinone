"""Several Claude accounts (Pro / Max / Team...) for the direct Claude option.

Each account has its own Claude Code configuration folder, so each keeps its own
sign-in. The projects themselves never live in an account: files are in your
folders and conversations in data/allinone.db, so a project moves from one
account to another simply by choosing the other account — Allinone starts a new
session there with a hand-over note of the work so far.

data/claude-accounts.json
  {"active": "default", "accounts": [{"id": "default", "name": "..."}, {"id": "work", "name": "Work"}]}
The "default" account uses the folder from claude.config_dir (data/claude-home).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .i18n import tr

DEFAULT = "default"


def _file(ctx) -> Path:
    return ctx.data_dir / "claude-accounts.json"


def load(ctx) -> Dict[str, Any]:
    try:
        data = json.loads(_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    accounts = [a for a in data.get("accounts") or [] if isinstance(a, dict) and a.get("id")]
    if not any(a["id"] == DEFAULT for a in accounts):
        accounts.insert(0, {"id": DEFAULT, "name": tr("Main account", "الحساب الأساسي")})
    active = data.get("active") if any(a["id"] == data.get("active") for a in accounts) else DEFAULT
    return {"active": active, "accounts": accounts}


def save(ctx, data: Dict[str, Any]) -> None:
    f = _file(ctx)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def exists(ctx, account_id: Optional[str]) -> bool:
    return bool(account_id) and any(a["id"] == account_id for a in load(ctx)["accounts"])


def resolve(ctx, account_id: Optional[str]) -> str:
    """The account to use: the one asked for if it exists, otherwise the active one."""
    return account_id if exists(ctx, account_id) else load(ctx)["active"]


def config_dir(ctx, account_id: Optional[str], default_dir: Path) -> Path:
    acc = resolve(ctx, account_id)
    if acc == DEFAULT:
        return default_dir
    return ctx.data_dir / "claude-accounts" / acc


def add(ctx, name: str) -> str:
    data = load(ctx)
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "account"
    acc_id, n = base, 2
    ids = {a["id"] for a in data["accounts"]}
    while acc_id in ids:
        acc_id, n = f"{base}-{n}", n + 1
    data["accounts"].append({"id": acc_id, "name": name.strip()[:60] or acc_id, "created": time.time()})
    save(ctx, data)
    (ctx.data_dir / "claude-accounts" / acc_id).mkdir(parents=True, exist_ok=True)
    return acc_id


def remove(ctx, account_id: str) -> None:
    if account_id == DEFAULT:
        raise ValueError(tr("The main account cannot be removed (sign out of it instead).",
                            "لا يمكن حذف الحساب الأساسي (سجّل الخروج منه بدلاً من ذلك)."))
    data = load(ctx)
    data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
    if data["active"] == account_id:
        data["active"] = DEFAULT
    save(ctx, data)
    shutil.rmtree(ctx.data_dir / "claude-accounts" / account_id, ignore_errors=True)


def activate(ctx, account_id: str) -> None:
    data = load(ctx)
    if not any(a["id"] == account_id for a in data["accounts"]):
        raise ValueError(tr("Unknown account.", "حساب غير معروف."))
    data["active"] = account_id
    save(ctx, data)


def session_key(gw, account_id: Optional[str]) -> str:
    """Claude Code sessions belong to one gateway — and, for Claude, to one account."""
    if getattr(gw, "type_name", "") != "anthropic" or not account_id or account_id == DEFAULT:
        return gw.name
    return f"{gw.name}#{account_id}"


def listing(ctx, with_status: bool = False) -> List[Dict[str, Any]]:
    out = []
    data = load(ctx)
    for a in data["accounts"]:
        item = {"id": a["id"], "name": a["name"], "active": a["id"] == data["active"]}
        if with_status:
            st = ctx.claude().auth_status(account=a["id"])
            item.update(loggedIn=bool(st.get("loggedIn")), email=st.get("email") or st.get("account") or "",
                        plan=st.get("subscriptionType") or st.get("plan") or "")
        out.append(item)
    return out
