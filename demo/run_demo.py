"""The real app UI on made-up data: `py -3.10 demo/run_demo.py [--no-window] [--state NAME]`.

For looking at the hub with computer use or showing it to someone: every message, name and org here is
invented, and the parts that reach real things are replaced before the server starts. It reads no chats,
orgs.json, secrets or .env, and it has no bot, no browser, no sync and no model calls. Its files live in
data/demo/ and are rebuilt on every start, so dates stay relative to today.

--state shows a state that's hard to reach by hand: syncing, stopping, live, paused, empty, huge, stale, phase2.
"""

import argparse
import datetime as dt
import json
import os
import random
import shutil
import struct
import sys
import threading
import time
import webbrowser
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "data", "demo")
PORT = 8799
TITLE = "Messenger Hub DEMO (made-up data)"

ME = "Riley Santos"
NOW = dt.datetime.now().replace(second=0, microsecond=0)
ORGS = {
    "north": ("North Council", "VP Comms", ["Vice President for Communications"], ["Design", "Media"], ["Mika Tan", "Paolo Reyes"]),
    "robo": ("Robotics Society", "AVP Creatives", ["Creatives AVP"], ["Publicity"], ["Noor Aziz"]),
    "game": ("Moon Game", "Lead dev", [], [], []),
}
CHATS = {
    "north": [("n-board", "[North] Board"), ("n-design", "[North] Design team"), ("n-week", "[North] Welcome Week")],
    "robo": [("r-exec", "[Robo] Exec"), ("r-pub", "[Robo] Publicity hub")],
    "game": [("g-team", "Moon Game team")],
}
PEOPLE = ["Mika Tan", "Paolo Reyes", "Noor Aziz", "Lea Cruz", "Jun Bautista", "Tess Ong", "Marco Lim", "Ana Villa", "Ben Ortiz"]
CHATTER = ["ok noted", "haha same", "thanks!", "sige", "wait lang", "nice one", "ok po", "see you later", "ang galing", "lunch muna",
           "pa-send na lang sa drive", "got it", "omw", "sorry late reply", "yes", "pwede", "next time na", "congrats team"]


def day(offset: int, hour: int = 17) -> dt.datetime:
    return (NOW + dt.timedelta(days=offset)).replace(hour=hour, minute=0)


def date_words(when: dt.datetime) -> str:
    return when.strftime("%B ") + str(when.day)


def stories() -> list:
    """(chat, minutes ago, sender, text, extras): the things worth seeing, each as it would be said in a chat."""
    h, d = 60, 1440
    return [
        # waiting on you, with a reporting-line sender, a date, a photo to check, a link
        ("n-board", 35, "Mika Tan", f"@Riley can you send the sponsor deck tonight? Deadline {date_words(day(0))} 9pm", {}),
        ("n-design", 95, "Lea Cruz", "VP Comms, pa-check naman this pubmat for the fair", {"image": "poster.png"}),
        ("n-design", 94, "Lea Cruz", "two versions, which one?", {"image": "poster2.png"}),
        ("r-exec", 3 * h, "Noor Aziz", "@Riley can you confirm the venue for Friday's GA 7pm?", {}),
        ("r-pub", 5 * h, "Jun Bautista", "AVP Creatives pwede ba ma-approve yung caption bukas before 10am", {"link": "https://example.com/caption-draft"}),
        ("g-team", 7 * h, "Marco Lim", "@Riley can you review the moon level build? due Sunday", {"link": "https://example.com/moon-build"}),
        ("n-week", 26 * h, "Paolo Reyes", "Riley can you post the Welcome Week schedule by tomorrow 12pm?", {}),
        ("n-board", 30 * h, "Tess Ong", "@Riley pa-review ng budget slides please", {}),
        # you answered, or you said you'd do it
        ("n-board", 2 * d, "Mika Tan", "@Riley can you join the alignment call Thursday 8pm?", {}),
        ("n-board", 2 * d - 20, ME, "yes i'll be there", {}),
        ("r-exec", 40 * h, "Noor Aziz", "@Riley who's handling the banner?", {}),
        ("r-exec", 40 * h - 15, ME, "ako na, i'll send it tomorrow", {}),
        ("n-week", 3 * d, "Ana Villa", "@Riley can you send the photo list?", {}),
        ("n-week", 3 * d - 40, ME, "will do later tonight", {}),
        # for everyone
        ("n-board", 6 * h, "Paolo Reyes", "@everyone GA is on " + date_words(day(4)) + " at 7pm, Hall B", {}),
        ("r-exec", 9 * h, "Noor Aziz", f"Org report deadline {date_words(day(6))} 11:59pm", {}),
        ("n-week", 12 * h, "Ana Villa", f"Meeting {date_words(day(2))} 6pm for booth layout", {}),
        ("g-team", 20 * h, "Ben Ortiz", f"playtest {date_words(day(9))} 3pm, join if free", {}),
        # went past their date while waiting
        ("n-week", 25 * h + d, "Ana Villa", f"Submit the booth form deadline {date_words(day(-1))} 5pm", {}),
        ("r-pub", 30 * h + d, "Tess Ong", f"Meeting {date_words(day(-1))} 7pm with the printer", {}),
        ("n-design", 28 * h, "Lea Cruz", f"Asset pack due {date_words(day(-1))} 8pm", {}),
    ]


def png(path: str, rgb: tuple) -> None:
    """A plain coloured picture, standing in for a poster."""
    w, h = 320, 240
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def chat_rows(slug: str, rng: random.Random, tell: list) -> list:
    rows = [(m, who, text, extra) for chat, m, who, text, extra in tell if chat == slug]
    for _ in range(90):  # ordinary chatter across a month, so conversations split as they do in real chats
        rows.append((rng.randint(10, 30 * 1440), rng.choice(PEOPLE), rng.choice(CHATTER), {}))
    rows.sort(key=lambda r: -r[0])
    out = []
    for i, (minutes, who, text, extra) in enumerate(rows, 1):
        m = {"kind": "message", "id": str(i), "ts": (NOW - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds"), "sender": who, "text": text}
        if extra.get("image"):
            m["attachments"] = [{"type": "image", "path": "attachments/" + extra["image"]}]
        if extra.get("link"):
            m["links"] = [extra["link"]]
        out.append(m)
    return out


def build(state: str) -> dict:
    shutil.rmtree(DEMO, ignore_errors=True)
    os.makedirs(os.path.join(DEMO, "chats"))
    rng = random.Random(7)
    tell = [] if state == "empty" else stories()
    if state == "huge":
        for i in range(260):  # a backlog: long titles, every org, due dates either side of today
            slug = rng.choice([s for chats in CHATS.values() for s, _ in chats])
            when = day(rng.randint(-1, 20))
            tell.append((slug, rng.randint(60, 2880), rng.choice(PEOPLE),
                         f"Deadline {date_words(when)} 5pm for the very long named deliverable number {i} of the Welcome Week committee handover pack", {}))
    orgs = {}
    for key, (name, title, aliases, committees, above) in ORGS.items():
        chats = []
        for slug, chat in CHATS[key]:
            folder = os.path.join(DEMO, "chats", slug)
            os.makedirs(os.path.join(folder, "attachments"))
            for n, colour in (("poster.png", (36, 89, 201)), ("poster2.png", (209, 100, 42))):
                png(os.path.join(folder, "attachments", n), colour)
            with open(os.path.join(folder, "messages.jsonl"), "w", encoding="utf-8") as f:
                for m in chat_rows(slug, rng, tell) if state != "empty" else []:
                    f.write(json.dumps(m) + "\n")
            chats.append({"thread": "demo-" + slug, "name": chat, "slug": slug, "enabled": True})
        orgs[key] = {"name": name, "match": [], "context": "", "outputs": [], "my_title": title, "aliases": aliases,
                     "committees": committees, "above": above, "chats": chats}
    cfg = {"me": ME, "profile": {"names": [ME, "Riley"], "not_me": ["Riley Ty"], "self_chat": None, "self_slug": None}, "settings": {}, "orgs": orgs}
    with open(os.path.join(DEMO, "orgs.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    return cfg


class FakeBot:
    def __init__(self, *a):
        self.last_error = None

    def start(self):
        pass

    def notice(self, *a):
        pass

    def send(self, *a):
        return True

    def status(self):
        return {"from_env": [], "has_token": True, "paired": True, "bot_name": "demo_bot", "pair_code": "000000", "error": None}

    def set_token(self, *a):
        pass

    def unpair(self):
        pass


class FakeWorker:
    """Reports a chosen state. Sync finishes after a few seconds and reads nothing."""

    def __init__(self, state: str):
        self.state, self.syncing = state, state in ("syncing", "stopping")

    def start(self):
        pass

    def stop(self):
        pass

    def wake(self):
        pass

    def request_audit(self):
        pass

    def resume(self):
        self.state = "live"

    def request_sync(self, every_chat=False):
        self.syncing = True
        threading.Timer(4, lambda: setattr(self, "syncing", False)).start()

    def stop_sync(self):
        self.syncing = False

    def status(self):
        now = NOW.isoformat()
        if self.state == "live":
            return {"state": "live", "last_check": now, "opens_today": 12, "limit": 200}
        if self.state == "paused":
            return {"state": "paused", "last_check": now, "opens_today": 12, "limit": 200, "needs_you": True, "why": "Facebook is asking for a login. Log in, then press Resume."}
        return {"state": "stopped"}


def isolate(state: str):
    """Point everything at data/demo/ and take the real connections out. Call before the app starts."""
    os.environ["HUB_DB"] = os.path.join(DEMO, "hub.db")
    os.environ["HUB_CHATS"] = os.path.join(DEMO, "chats")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        os.environ.pop(k, None)
    sys.path.insert(0, ROOT)
    import hub
    from hubos import app, live, process, settings, store, threads

    hub.CONFIG = os.path.join(DEMO, "orgs.json")
    hub.DATA, hub.CHATS, hub.ORGS = DEMO, process.CHATS, os.path.join(DEMO, "orgs")
    hub.CHAT_LIST, hub.LIST_STATE = os.path.join(DEMO, "chat-list.json"), os.path.join(DEMO, "list-state.json")
    hub.STOP_FILE = os.path.join(DEMO, "STOP")
    settings.SECRETS, settings.ENV_FILE = os.path.join(DEMO, "secrets.json"), os.path.join(DEMO, ".env")
    live.DIR = os.path.join(DEMO, "live")
    app.STARTUP_FILE, app.DESKTOP_FILE = os.path.join(DEMO, "startup.vbs"), os.path.join(DEMO, "desktop.vbs")
    app.run_sync = lambda every_chat=False: None
    threads.summarise = lambda st, card, me: None
    webbrowser.open = lambda url, *a, **k: print("[demo] would open a link")
    for path in (store.DB, process.CHATS, hub.CONFIG, settings.SECRETS):
        if not os.path.realpath(path).startswith(os.path.realpath(DEMO) + os.sep):
            raise SystemExit(f"Refusing to start: {path} isn't in data/demo/")
    return hub, app, process


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--bar", action="store_true", help="open as the floating bar")
    ap.add_argument("--state", default="normal", choices=["normal", "syncing", "stopping", "live", "paused", "empty", "huge", "stale", "phase2"])
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    cfg = build(a.state)
    hub, app, process = isolate(a.state)
    import http.server

    from hubos.store import Store

    app.STORE = Store()
    process.ingest(app.STORE, cfg, force=True)  # the real rules, over the made-up chats
    if a.state == "stale":
        old = time.time() - 3 * 86400
        for chats in CHATS.values():
            for slug, _ in chats:
                os.utime(os.path.join(DEMO, "chats", slug, "messages.jsonl"), (old, old))
    if a.state == "phase2":  # what Canvas and the class schedule will look like when they arrive
        for kind, title, when in (("class", "Engineering Economics", day(0, 22)), ("canvas", "Problem set 4", day(1, 23)), ("class", "Data Structures", day(1, 9))):
            app.STORE.add_entry(kind=kind, title=title, due=when.isoformat(timespec="minutes"), due_has_time=1, org=None, item_id=None, ref=None,
                                status="open", origin="canvas", review="accepted", confidence=1, reasons="")
    app.BOT, app.WORKER = FakeBot(), FakeWorker(a.state)
    if a.state == "stopping":
        with open(hub.STOP_FILE, "w"):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Demo running at {url}", flush=True)
    if a.no_window:
        while True:
            time.sleep(3600)
    import webview

    api = app.WindowApi(url, TITLE)
    api.main_window()
    webview.start(api.open_bar if a.bar else None)


if __name__ == "__main__":
    main()
