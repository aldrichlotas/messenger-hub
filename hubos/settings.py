"""Settings (orgs.json "settings"/"profile") and secrets (data/secrets.json, git-ignored)."""

import copy
import json
import os
import random

from .store import ROOT

SECRETS = os.path.join(ROOT, "data", "secrets.json")

DEFAULTS = {
    "briefs": {
        "morning": {"on": True, "at": "07:00"},
        "afternoon": {"on": True, "at": "13:00"},
        "night": {"on": True, "at": "21:00"},
    },
    "quiet": {"start": "23:00", "end": "07:00"},
    "alerts": True,
    "ping_cap": 5,  # alert messages per day (briefs don't count)
    "detail": "short",  # what Telegram may show: "short" (cleaned titles) or "minimal" (org, type and time only)
    "process_minutes": 5,  # how often the app re-reads synced chats (no tokens)
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def get(cfg: dict) -> dict:
    return _merge(DEFAULTS, cfg.get("settings") or {})


def profile(cfg: dict) -> dict:
    p = dict(cfg.get("profile") or {})
    p.setdefault("names", [cfg.get("me")] if cfg.get("me") and cfg.get("me") != "You" else [])
    p.setdefault("self_chat", None)
    p.setdefault("self_slug", None)
    return p


def load_secrets() -> dict:
    if os.path.exists(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    if not data.get("pair_code"):
        data["pair_code"] = f"{random.randint(0, 999999):06d}"
        save_secrets(data)
    return data


def save_secrets(data: dict) -> None:
    os.makedirs(os.path.dirname(SECRETS), exist_ok=True)
    with open(SECRETS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
