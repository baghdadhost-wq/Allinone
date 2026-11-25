"""Manage FreeLLMAPI's provider keys from Allinone (its dashboard API, on this computer only).

What it is for: Claude Code needs models that can call tools, and FreeLLMAPI only
routes to providers you have a key for. This module shows which providers offer
tool-capable models, opens their key pages, adds the key you paste, enables the
providers that need no key at all (FreeLLMAPI's own "keyless" feature), and picks
the fastest usable tool-capable model.

Dashboard sign-in: on a fresh FreeLLMAPI, Allinone creates the local dashboard
account itself (stored in data/secrets.json). If you already created one, you
type its e-mail and password once in the keys page.
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import netutil
from .i18n import tr

# Key pages for the best-known providers, in case the installed source does not list them.
FALLBACK_URLS = {
    "google": "https://aistudio.google.com/apikey", "groq": "https://console.groq.com/keys",
    "cerebras": "https://cloud.cerebras.ai", "openrouter": "https://openrouter.ai/keys",
    "mistral": "https://console.mistral.ai/api-keys/", "nvidia": "https://build.nvidia.com/settings/api-keys",
    "cohere": "https://dashboard.cohere.com/api-keys", "cloudflare": "https://dash.cloudflare.com",
    "zhipu": "https://z.ai/manage-apikey/apikey-list", "huggingface": "https://huggingface.co/settings/tokens",
}


class AdminError(RuntimeError):
    pass


class NeedCredentials(AdminError):
    """The dashboard account exists but Allinone does not know its password."""


class FreeLLMAPIAdmin:
    _token_cache: Dict[str, Tuple[str, float]] = {}

    def __init__(self, ctx, gw):
        self.ctx, self.gw = ctx, gw
        self.base = gw.base_url.rstrip("/")

    # ------------------------------------------------------------------ sign-in
    def _call(self, method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None,
              timeout: float = 20.0) -> Tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            status, raw = netutil.request(method, self.base + path, headers=headers, body=body, timeout=timeout)
        except Exception as exc:
            raise AdminError(tr("FreeLLMAPI is not answering ({e}). Start it first.",
                                "FreeLLMAPI لا يستجيب ({e}). شغّله أولاً.", e=exc)) from exc
        try:
            data = json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            data = None
        return status, data

    def save_credentials(self, email: str, password: str) -> None:
        self.ctx.secrets.set(self.gw.name, "dashboard_email", email.strip())
        self.ctx.secrets.set(self.gw.name, "dashboard_password", password)
        self._token_cache.pop(self.gw.name, None)

    def token(self) -> str:
        cached = self._token_cache.get(self.gw.name)
        if cached and cached[1] > time.time():
            return cached[0]
        status, data = self._call("GET", "/api/auth/status")
        if status != 200 or not isinstance(data, dict):
            raise AdminError(tr("FreeLLMAPI dashboard did not answer.", "لوحة FreeLLMAPI لم تستجب."))
        email = self.ctx.secrets.get(self.gw.name, "dashboard_email")
        password = self.ctx.secrets.get(self.gw.name, "dashboard_password")
        if data.get("needsSetup"):
            # A fresh install: claim the local dashboard for Allinone (allowed only from this computer).
            email, password = "allinone@localhost.local", _secrets.token_urlsafe(18)
            status, res = self._call("POST", "/api/auth/setup", {"email": email, "password": password})
            if status not in (200, 201) or not (res or {}).get("token"):
                raise AdminError(tr("Could not create the FreeLLMAPI dashboard account: {e}",
                                    "تعذّر إنشاء حساب لوحة FreeLLMAPI: {e}", e=_msg(res)))
            self.save_credentials(email, password)
            tok = res["token"]
        else:
            if not email or not password:
                raise NeedCredentials(tr("Type the FreeLLMAPI dashboard e-mail and password once.",
                                         "اكتب بريد وكلمة مرور لوحة FreeLLMAPI مرة واحدة."))
            status, res = self._call("POST", "/api/auth/login", {"email": email, "password": password})
            if status != 200 or not (res or {}).get("token"):
                if status in (400, 401):
                    raise NeedCredentials(tr("The saved FreeLLMAPI dashboard password is not accepted.",
                                             "كلمة مرور لوحة FreeLLMAPI المحفوظة غير مقبولة."))
                raise AdminError(_msg(res) or f"HTTP {status}")
            tok = res["token"]
        self._token_cache[self.gw.name] = (tok, time.time() + 6 * 3600)
        return tok

    def api(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 30.0) -> Any:
        status, data = self._call(method, path, body, token=self.token(), timeout=timeout)
        if status == 401:  # session expired: sign in again once
            self._token_cache.pop(self.gw.name, None)
            status, data = self._call(method, path, body, token=self.token(), timeout=timeout)
        if status >= 400:
            raise AdminError(_msg(data) or f"HTTP {status}")
        return data

    # ------------------------------------------------------------------ providers
    def key_urls(self) -> Dict[str, Dict[str, Any]]:
        """Sign-up/key pages from the installed FreeLLMAPI dashboard source."""
        out: Dict[str, Dict[str, Any]] = {k: {"url": v} for k, v in FALLBACK_URLS.items()}
        src = Path(self.gw.install_dir) / "client" / "src" / "components" / "keys" / "shared.tsx"
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            return out
        for m in re.finditer(r"\{\s*value:\s*'([^']+)',\s*label:\s*'([^']*)',\s*url:\s*'([^']*)'([^}]*)\}", text):
            rest = m.group(4)
            out[m.group(1)] = {"label": m.group(2), "url": m.group(3), "hidden": "hidden: true" in rest,
                               "keyless": "keyless: true" in rest}
        return out

    def models(self) -> List[Dict[str, Any]]:
        data = self.api("GET", "/api/models")
        return data if isinstance(data, list) else (data or {}).get("models") or []

    def status(self) -> Dict[str, Any]:
        provs = (self.api("GET", "/api/keys/providers") or {}).get("providers") or []
        models = self.models()
        urls = self.key_urls()
        tools_by_platform: Dict[str, int] = {}
        for m in models:
            if m.get("enabled", True) and m.get("supportsTools"):
                tools_by_platform[m.get("platform")] = tools_by_platform.get(m.get("platform"), 0) + 1
        configured = {p["platform"] for p in provs if p.get("enabledKeyCount")}
        rows = []
        for p in provs:
            if p.get("kind") == "search":
                continue
            info = urls.get(p["platform"], {})
            if info.get("hidden") and not p.get("configured"):
                continue
            rows.append({"platform": p["platform"], "name": p.get("name") or info.get("label") or p["platform"],
                         "url": info.get("url") or "", "keyless": bool(p.get("keyless")),
                         "configured": p["platform"] in configured, "keys": p.get("enabledKeyCount") or 0,
                         "tool_models": tools_by_platform.get(p["platform"], 0)})
        rows.sort(key=lambda r: (r["configured"], -r["tool_models"], r["name"].lower()))
        try:
            keyless = (self.api("GET", "/api/keys/keyless-available") or {}).get("providers") or []
        except AdminError:
            keyless = []
        usable = [m for m in models if m.get("enabled", True) and m.get("supportsTools") and m.get("platform") in configured]
        return {"providers": rows, "usable_tool_models": len(usable),
                "keyless_available": [k.get("name") or k.get("platform") if isinstance(k, dict) else str(k) for k in keyless]}

    def add_key(self, platform: str, key: str) -> Dict[str, Any]:
        res = self.api("POST", "/api/keys", {"platform": platform, "key": key.strip()})
        return {"models": (res or {}).get("modelsAvailable"), "notice": (res or {}).get("notice") or "",
                "status": (res or {}).get("status") or ""}

    def enable_keyless(self) -> Any:
        return (self.api("POST", "/api/keys/keyless-available/enable") or {}).get("enabled")

    # ------------------------------------------------------------------ fastest model
    def tool_models(self, min_context: int = 0, exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        """Usable models that can call tools, fastest first (FreeLLMAPI's own speed ranking)."""
        provs = (self.api("GET", "/api/keys/providers") or {}).get("providers") or []
        configured = {p["platform"] for p in provs if p.get("enabledKeyCount")}
        exclude = exclude or set()
        cands = [m for m in self.models() if m.get("enabled", True) and m.get("supportsTools")
                 and m.get("platform") in configured and m.get("modelId") not in exclude
                 and int(m.get("contextWindow") or 0) >= min_context]
        cands.sort(key=lambda m: (m.get("speedRank") or 999, -(m.get("contextWindow") or 0), m.get("intelligenceRank") or 999))
        return cands


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")
        return str(err or data.get("message") or "")
    return ""
