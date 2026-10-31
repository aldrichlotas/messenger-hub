"""Telegram bot (your phone surface) and the scheduler for briefs and alerts. No tokens.

The bot only talks to the one Telegram chat that paired with it (`/start <code>`, the code shown in
the app's settings). Messages are plain text and never include message bodies from your chats,
other people's names, links or contact details: titles are cleaned, or omitted in "minimal" mode.
"""

import datetime as dt
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import process, settings
from .store import Store

API = "https://api.telegram.org/bot{token}/{method}"
HELP = (
    "Messenger Hub\n"
    "Send any text to save it as a note (dates like 'bukas 7pm' or 'Fri' set a due time; #ecg tags an org).\n"
    "/today  what's on today\n"
    "/due  next deadlines\n"
    "/done 12  mark #12 done\n"
    "/win text  log an achievement\n"
    "/review  how many suggestions wait on your laptop"
)


# --- formatting ---------------------------------------------------------------------------------


def _clock(t: dt.datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def when_label(e: dict, now: dt.datetime) -> str:
    if not e.get("due"):
        return "no date"
    d = dt.datetime.fromisoformat(e["due"])
    days = (d.date() - now.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else "yesterday" if days == -1 else d.strftime("%a %b %d")
    return f"{day} {_clock(d)}" if e.get("due_has_time") else day


TLD = r"(?:com|org|net|edu|gov|ph|io|co|me|ly|gle|gl|to|app|site|xyz)"


def people(st: Store, names: list) -> list:
    """Name words of everyone else in the synced chats, for safe_title (Messenger mentions often have no @)."""
    mine = {w.lower() for x in names for w in x.split()}
    words = {w for r in st.q("SELECT DISTINCT author FROM item WHERE is_me = 0 AND author IS NOT NULL")
             for w in re.findall(r"[^\W\d_][\w'-]*", r["author"]) if w[0].isupper() and w.lower() not in mine}
    return sorted(words, key=len, reverse=True)


def safe_title(e: dict, names: list, n: int = 60, others=()) -> str:
    """A title fit for Telegram: no links, emails, phone numbers, other people's @mentions or names in `others`."""
    t = e.get("title") or ""
    t = re.sub(r"https?://\S+|\[link\]", "", t)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "", t)
    t = re.sub(rf"\bwww\.\S+|\b[\w-]+(?:\.[\w-]+)+/\S*|\b[\w-]+(?:\.[\w-]+)*\.{TLD}\b", "", t)  # links without https://
    t = re.sub(r"(\+?\d[\d\s.-]{7,}\d)", "", t)
    mine = {x.split()[0].lower() for x in names if x.strip()}
    t = re.sub(r"@([^\W\d_][\w.]*(?:\s[A-ZÀ-ÖØ-Þ][\w.]*)*)", lambda m: m.group(0) if m.group(1).split()[0].lower() in mine else "@…", t)
    if others:
        t = re.sub(r"(?<![@\w])(?:%s)\b" % "|".join(map(re.escape, others)), "…", t)
        t = re.sub(r"…(?:\s+…)+", "…", t)  # "Sam Reyes" -> one …
    t = re.sub(r"\s+", " ", t).strip(" -·,:")
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


KIND = {"deadline": "deadline", "meeting": "meeting", "task": "task", "capture": "note", "win": "win"}


def line(e: dict, now: dt.datetime, detail: str, names: list, others=()) -> str:
    org = (e.get("org") or "personal").upper()
    head = f"#{e['id']} {org} · {KIND.get(e['kind'], e['kind'])} · {when_label(e, now)}"
    if detail == "minimal" and e.get("origin") == "rule":
        return head
    title = safe_title(e, names, others=others)
    return f"{head}\n   {title}" if title else head


# --- the bot ------------------------------------------------------------------------------------


class Bot:
    def __init__(self, store: Store, get_cfg):
        self.st, self.get_cfg = store, get_cfg
        self._stop = threading.Event()
        self.threads = []
        self.last_error = None
        self._named = False

    # Telegram API
    def _secrets(self):
        return settings.load_secrets()

    def api(self, method: str, http_timeout: int = 30, **params):
        token = self._secrets().get("telegram_token")
        if not token:
            raise RuntimeError("no bot token")
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(API.format(token=token, method=method), data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # Telegram explains 4xx errors in the body
            try:
                out = json.loads(e.read().decode("utf-8"))
            except ValueError:
                raise RuntimeError(f"Telegram HTTP {e.code}") from None
            if "chat not found" in (out.get("description") or ""):
                out["description"] = "chat not found: open your bot in Telegram and press Start once"
        if not out.get("ok"):
            raise RuntimeError(out.get("description") or "Telegram error")
        return out["result"]

    def send(self, text: str, chat_id=None) -> bool:
        chat_id = chat_id or self._secrets().get("telegram_chat_id")
        if not chat_id:
            return False
        try:
            self.api("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)
            return True
        except Exception as e:  # keep running; shown in the app
            self.last_error = str(e)
            return False

    def status(self) -> dict:
        s = self._secrets()
        if not self._named and s.get("telegram_token") and not s.get("bot_name") and "telegram_token" in s.get("from_env", []):
            self._named = True
            try:  # token came from .env: look up the bot's name once
                s["bot_name"] = self.api("getMe", http_timeout=10).get("username")
                settings.save_secrets(s)
            except Exception as e:
                self.last_error = str(e)
        return {
            "from_env": s.get("from_env", []),
            "has_token": bool(s.get("telegram_token")),
            "paired": bool(s.get("telegram_chat_id")),
            "pair_code": s.get("pair_code"),
            "bot_name": s.get("bot_name"),
            "error": self.last_error,
        }

    def set_token(self, token: str) -> dict:
        s = self._secrets()
        s["telegram_token"] = token.strip()
        s.pop("telegram_chat_id", None)
        s["pair_code"] = None
        settings.save_secrets(s)
        s = settings.load_secrets()  # new pair code
        try:
            me = self.api("getMe")
            s["bot_name"] = me.get("username")
            settings.save_secrets(s)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
        return self.status()

    def unpair(self) -> dict:
        s = self._secrets()
        s.pop("telegram_chat_id", None)
        s["pair_code"] = None
        settings.save_secrets(s)
        settings.load_secrets()
        return self.status()

    # incoming messages
    def poll_forever(self):
        while not self._stop.is_set():
            if not self._secrets().get("telegram_token"):
                time.sleep(5)
                continue
            try:
                offset = int(self.st.get_meta("tg_offset", 0))
                updates = self.api("getUpdates", http_timeout=40, offset=offset, timeout=25, allowed_updates=["message"])
                for u in updates:
                    self.st.set_meta("tg_offset", u["update_id"] + 1)
                    msg = u.get("message") or {}
                    if msg.get("text"):
                        self.handle(msg["chat"]["id"], msg["text"].strip())
                self.last_error = None
            except urllib.error.URLError as e:
                self.last_error = f"offline: {e.reason}"
                time.sleep(15)
            except Exception as e:
                self.last_error = str(e)
                time.sleep(10)

    def handle(self, chat_id: int, text: str) -> None:
        s = self._secrets()
        cfg = self.get_cfg()
        if not s.get("telegram_chat_id"):
            m = re.match(r"^/start\s+(\d{6})$", text)
            if m and m.group(1) == s.get("pair_code"):
                s["telegram_chat_id"] = chat_id
                settings.save_secrets(s)
                self.send("Paired with Messenger Hub. " + HELP, chat_id)
            else:
                self.send("This bot is private. Pair it with /start <code> from the Messenger Hub app.", chat_id)
            return
        if chat_id != s.get("telegram_chat_id"):
            return  # someone else found the bot: ignore
        now = dt.datetime.now()
        names = settings.profile(cfg)["names"]
        others = people(self.st, names)
        detail = settings.get(cfg)["detail"]
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        if cmd in ("/help", "/start"):
            self.send(HELP)
        elif cmd == "/today":
            self.send(compose_brief(self.st, cfg, "today", now) or "Nothing due today.")
        elif cmd == "/due":
            d = process.dashboard(self.st, now, cfg)
            items = d["overdue"] + d["deadlines"]
            self.send("\n".join(line(e, now, detail, names, others) for e in items[:12]) or "No upcoming deadlines.")
        elif cmd == "/review":
            d = process.dashboard(self.st, now, cfg)
            self.send(f"{d['review_count']} suggestions and {d['unread_mentions']} conversations that mention you wait on your laptop.")
        elif cmd == "/done":
            m = re.match(r"#?(\d+)", rest.strip())
            e = self.st.one("SELECT * FROM entry WHERE id = ?", (int(m.group(1)),)) if m else None
            if not e:
                self.send("Which one? e.g. /done 12 (numbers are shown in /today and /due).")
            else:
                self.st.update_entry(e["id"], status="done", review="accepted")
                self.send(f"Done: #{e['id']} {safe_title(e, names, others=others)}")
        elif cmd == "/win":
            if not rest.strip():
                self.send("Tell me what you did: /win led the GA pubmats, 14 posts #ecg")
                return
            e = process.capture(self.st, "/win " + rest, "telegram-bot", cfg)
            self.send(f"Win logged: {safe_title(e, names, 80, others)}")
        elif cmd.startswith("/"):
            self.send(HELP)
        else:
            e = process.capture(self.st, text, "telegram-bot", cfg)
            self.send(f"Saved #{e['id']}: {safe_title(e, names, 80, others)} · {when_label(e, now)}")

    # scheduler
    def tick(self, now: dt.datetime = None) -> list:
        """Send due briefs and alerts. Returns what was sent (for tests)."""
        now = now or dt.datetime.now()
        cfg = self.get_cfg()
        sset = settings.get(cfg)
        sent = []
        if not self._secrets().get("telegram_chat_id") or in_quiet(now, sset["quiet"]):
            return sent
        for name, b in sset["briefs"].items():
            if not b.get("on"):
                continue
            at = dt.datetime.combine(now.date(), dt.time.fromisoformat(b["at"]))
            key = f"brief:{now.date()}:{name}"
            # Catch up within 3 h (laptop was asleep), but never send a stale brief later than that.
            if at <= now < at + dt.timedelta(hours=3) and not self.st.was_sent(key):
                text = compose_brief(self.st, cfg, name, now)
                if text and self.send(text):
                    self.st.mark_sent(key)
                    sent.append(key)
        if sset["alerts"]:
            msgs = []
            for key, text in due_alerts(self.st, cfg, now):
                if self.st.sent_today("alert:") + len(msgs) >= int(sset["ping_cap"]):
                    break
                msgs.append((key, text))
            if msgs and self.send("⏰ " + "\n".join(t for _, t in msgs)):
                for key, _ in msgs:
                    self.st.mark_sent(key)
                    sent.append(key)
        return sent

    def notice(self, text: str, now: dt.datetime = None) -> bool:
        """A fixed hub message (no chat content) under the same rules as alerts: off, quiet hours, daily cap.
        False means it wasn't delivered, so the caller can try again later."""
        now = now or dt.datetime.now()
        sset = settings.get(self.get_cfg())
        if not sset["alerts"] or in_quiet(now, sset["quiet"]) or self.st.sent_today("alert:") >= int(sset["ping_cap"]):
            return False
        if not self.send(text):
            return False
        self.st.mark_sent(f"alert:notice:{now.isoformat()}")
        return True

    def schedule_forever(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.last_error = f"scheduler: {e}"
            self._stop.wait(30)

    def start(self):
        for fn in (self.poll_forever, self.schedule_forever):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self._stop.set()


def in_quiet(now: dt.datetime, quiet: dict) -> bool:
    start, end, t = dt.time.fromisoformat(quiet["start"]), dt.time.fromisoformat(quiet["end"]), now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def due_alerts(st: Store, cfg: dict, now: dt.datetime) -> list:
    """(sent-key, text) for alerts that are due now: meetings in 30 min, things due within 2 h / 24 h."""
    names = settings.profile(cfg)["names"]
    others = people(st, names)
    detail = settings.get(cfg)["detail"]
    soon = (now + dt.timedelta(hours=24)).isoformat(timespec="minutes")
    out = []
    for e in process.entries(st, f"e.status = 'open' AND e.due IS NOT NULL AND e.due >= ? AND e.due <= ? AND {process.RELEVANT}", (now.isoformat(timespec="minutes"), soon)):
        due = dt.datetime.fromisoformat(e["due"])
        left = due - now
        if e["kind"] == "meeting" and e["due_has_time"] and left <= dt.timedelta(minutes=30):
            key = f"alert:m30:{e['id']}"
        elif e["due_has_time"] and left <= dt.timedelta(hours=2):
            key = f"alert:d2:{e['id']}"
        elif e["kind"] != "meeting":
            key = f"alert:d24:{e['id']}"
        else:
            continue
        if not st.was_sent(key):
            out.append((key, line(e, now, detail, names, others)))
    return out


def compose_brief(st: Store, cfg: dict, name: str, now: dt.datetime) -> str:
    names = settings.profile(cfg)["names"]
    others = people(st, names)
    detail = settings.get(cfg)["detail"]
    d = process.dashboard(st, now, cfg)
    fmt = lambda es: [line(e, now, detail, names, others) for e in es]  # noqa: E731
    later_today = [e for e in d["today"] if e["due"] >= now.isoformat(timespec="minutes") or not e["due_has_time"]]
    parts = []
    if name in ("morning", "today"):
        title = f"☀️ {now:%a %b %d}" if name == "morning" else "Today"
        if d["today"]:
            parts += ["Today:"] + fmt(d["today"])
        else:
            parts.append("Nothing due today.")
        if d["overdue"]:
            parts += [f"Overdue ({len(d['overdue'])}):"] + fmt(d["overdue"][:3])
        if d["tomorrow"]:
            parts.append(f"Tomorrow: {len(d['tomorrow'])} item(s)")
    elif name == "afternoon":
        title = "🌤 Afternoon check"
        if not later_today and not d["tomorrow"] and not d["overdue"]:
            return ""  # nothing to say: stay quiet
        if later_today:
            parts += ["Rest of today:"] + fmt(later_today)
        if d["overdue"]:
            parts.append(f"Overdue: {len(d['overdue'])}")
        if d["tomorrow"]:
            parts += ["Tomorrow:"] + fmt(d["tomorrow"][:5])
    else:
        title = "🌙 Tomorrow"
        parts += fmt(d["tomorrow"]) or ["Nothing due tomorrow."]
        still = [e for e in d["today"]] + d["overdue"]
        if still:
            parts.append(f"Still open from today or earlier: {len(still)}")
    extra = []
    if d["inbox"]:
        extra.append(f"{len(d['inbox'])} undated notes")
    if d["unread_mentions"]:
        extra.append(f"{d['unread_mentions']} conversation{'s' if d['unread_mentions'] != 1 else ''} need{'' if d['unread_mentions'] != 1 else 's'} you")
    if d["review_count"]:
        extra.append(f"{d['review_count']} suggestions to review")
    if extra:
        parts.append("On your laptop: " + ", ".join(extra))
    return "\n".join([title] + parts)
