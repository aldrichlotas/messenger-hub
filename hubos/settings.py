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
    "summaries": "off",  # "lite": Haiku writes one line per conversation that still needs you (uses your Claude plan)
    "live": False,  # watch the chat list while the hub runs and fetch chats as they change (hubos/live.py)
    "live_limit": 300,  # chats live sync may open by itself per day; your Sync button doesn't count
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


ENV_FILE = os.path.join(ROOT, ".env")
# .env name -> secrets key. A private chat's id is the user's id, so TELEGRAM_USER_ID pairs the bot.
ENV_SECRETS = {"TELEGRAM_BOT_TOKEN": "telegram_token", "TELEGRAM_USER_ID": "telegram_chat_id"}


def read_env(path: str = None) -> dict:
    """KEY=value lines from .env (spaces around "=" and quotes allowed); real env vars win."""
    out, path = {}, path or ENV_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                out[k] = v.strip().strip("'\"")
    for k in list(out) + list(ENV_SECRETS):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def env_secrets(path: str = None) -> dict:
    env, out = read_env(path), {}
    for name, key in ENV_SECRETS.items():
        if env.get(name):
            out[key] = int(env[name]) if key == "telegram_chat_id" else env[name]
    return out


def load_secrets() -> dict:
    if os.path.exists(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    if not data.get("pair_code"):
        data["pair_code"] = f"{random.randint(0, 999999):06d}"
        save_secrets(data)
    env = env_secrets()
    data.update(env)
    data["from_env"] = sorted(env)
    return data


def save_secrets(data: dict) -> None:
    # Values that came from .env stay there; secrets.json keeps only what the app set itself.
    env = env_secrets()
    data = {k: v for k, v in data.items() if k != "from_env" and not (k in env and env[k] == v)}
    os.makedirs(os.path.dirname(SECRETS), exist_ok=True)
    with open(SECRETS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
