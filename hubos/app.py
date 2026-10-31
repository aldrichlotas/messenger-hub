"""The Messenger Hub app: `py -3.10 hub.py app`.

One process runs the local server (127.0.0.1 only), the background processor (re-reads synced chats
every few minutes, no tokens) and the Telegram bot + scheduler. The window is a native desktop
window (pywebview) around the page; closing it leaves the hub running so briefs still arrive.
Running `hub.py app` again just opens another window onto the running hub.
"""

import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import config_ui
import hub

from . import bot as botmod
from . import live, process, settings, threads
from .store import Store

PORT = 8765
BAR_WIDTH, BAR_SHUT, BAR_OPEN = 640, 48, 440
APP_UI = os.path.join(hub.ROOT, "ui", "app.html")
STARTUP_FILE = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Messenger Hub.vbs"
)

STORE = None
BOT = None
WORKER = None
_sync_lock = threading.Lock()
_stop = threading.Event()


# --- start with Windows -------------------------------------------------------------------------


def pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def launcher_vbs() -> str:
    """A tiny VBScript that starts the app without a console window."""
    return (
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.CurrentDirectory = "{hub.ROOT}"\n'
        f'sh.Run """{pythonw()}"" ""{os.path.join(hub.ROOT, "hub.py")}"" app", 0, False\n'
    )


DESKTOP_FILE = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "Messenger Hub.vbs")


def write_launcher(path: str) -> None:
    # Text mode turns "\n" into "\r\n" on Windows, which is what VBScript expects.
    with open(path, "w", encoding="utf-8") as f:
        f.write(launcher_vbs())


def set_startup(on: bool) -> bool:
    if on:
        write_launcher(STARTUP_FILE)
    elif os.path.exists(STARTUP_FILE):
        os.remove(STARTUP_FILE)
    return os.path.exists(STARTUP_FILE)


# --- state for the page -------------------------------------------------------------------------


def self_chat_candidates(cfg: dict) -> list:
    names = [n.lower() for n in settings.profile(cfg)["names"]] or [cfg.get("me", "").lower()]
    out = []
    for c in hub.load_chat_list()["chats"]:
        if not c.get("community") and any(n and n in c["name"].lower() for n in names):
            out.append({"name": c["name"], "url": c["url"], "preview": c.get("preview")})
    return out[:6]


def synced_at(cfg: dict):
    """When a chat last changed on disk: what a sync brought in, as opposed to the processor re-reading it."""
    times = []
    for slug in process.channels(cfg):
        path = os.path.join(process.CHATS, slug, "messages.jsonl")
        if os.path.exists(path):
            times.append(os.path.getmtime(path))
    return dt.datetime.fromtimestamp(max(times)).isoformat(timespec="seconds") if times else None


def app_state() -> dict:
    cfg = hub.load_config()
    d = process.dashboard(STORE, cfg=cfg)
    return {
        "dash": d,
        "synced_at": synced_at(cfg),
        "me": cfg.get("me"),
        "profile": settings.profile(cfg),
        "settings": settings.get(cfg),
        "orgs": {
            k: {f: o.get(f) for f in ("name", "my_title", "aliases", "committees", "above")} | {"chats": len(o.get("chats", []))}
            for k, o in cfg.get("orgs", {}).items()
        },
        "selfCandidates": self_chat_candidates(cfg),
        "telegram": BOT.status() if BOT else {},
        "startup": os.path.exists(STARTUP_FILE),
        "desktopShortcut": os.path.exists(DESKTOP_FILE),
        "syncing": _sync_lock.locked() or bool(WORKER and WORKER.syncing),
        "stopping": bool(WORKER and WORKER.syncing) and os.path.exists(hub.STOP_FILE),
        "live": WORKER.status() if WORKER else {"state": "stopped"},
    }


def save_profile(body: dict) -> None:
    cfg = hub.load_config()
    prof = cfg.setdefault("profile", {})
    if "names" in body:
        prof["names"] = [n.strip() for n in body["names"] if str(n).strip()]
        if prof["names"]:
            cfg["me"] = cfg.get("me") if cfg.get("me") not in (None, "You") else prof["names"][0]
    if "me" in body and str(body["me"]).strip():
        cfg["me"] = str(body["me"]).strip()
    if "self_chat" in body:
        new = (body["self_chat"] or "").strip() or None
        if new != prof.get("self_chat"):
            prof["self_chat"], prof["self_slug"] = new, None
    for key, o in (body.get("orgs") or {}).items():
        if key not in cfg.get("orgs", {}):
            continue
        org = cfg["orgs"][key]
        for f in ("my_title",):
            if f in o:
                org[f] = str(o[f] or "").strip() or None
        for f in ("aliases", "committees", "above"):
            if f in o:
                org[f] = [x.strip() for x in o[f] if str(x).strip()]
    hub.save_config(cfg)


def save_settings(body: dict) -> None:
    cfg = hub.load_config()
    cur = settings.get(cfg)
    for k in ("briefs", "quiet"):
        if k in body:
            cur[k] = settings._merge(cur[k], body[k])
    for k in ("alerts",):
        if k in body:
            cur[k] = bool(body[k])
    if "ping_cap" in body:
        cur["ping_cap"] = max(0, min(50, int(body["ping_cap"])))
    if body.get("detail") in ("short", "minimal"):
        cur["detail"] = body["detail"]
    if body.get("summaries") in ("off", "lite"):
        cur["summaries"] = body["summaries"]
    if "live" in body:
        cur["live"] = bool(body["live"])
    if "live_limit" in body:
        cur["live_limit"] = max(20, min(1000, int(body["live_limit"])))
    if "process_minutes" in body:
        cur["process_minutes"] = max(1, min(120, int(body["process_minutes"])))
    for b in cur["briefs"].values():
        dt.time.fromisoformat(b["at"])  # validate
    dt.time.fromisoformat(cur["quiet"]["start"]), dt.time.fromisoformat(cur["quiet"]["end"])
    cfg["settings"] = cur
    hub.save_config(cfg)


def find_cards(cfg: dict, key=None) -> list:
    cs = threads.cards(STORE, process.channels(cfg), process.profile(cfg))
    return [c for c in cs if key is None or c["key"] == key]


def linked_entries(card: dict) -> list:
    """The entries found in a conversation's pings: dealing with the conversation deals with them."""
    ids = ",".join(str(int(i)) for i in card["ping_items"]) or "0"
    return [r["id"] for r in STORE.q(f"SELECT id FROM entry WHERE item_id IN ({ids})")]


def resolve(cfg: dict, body: dict) -> None:
    """Done, or not for me: one action that closes the entry and its conversation together."""
    keep = body.get("how") != "drop"
    ids = {int(body["entry"])} if body.get("entry") else set()
    for c in find_cards(cfg, str(body["card"])) if body.get("card") else []:
        threads.mark_read(STORE, c["key"], c["last_ping"], c["ping_items"])
        threads.mark_done(STORE, c["key"], c["end"])
        ids.update(linked_entries(c))
    for i in ids:
        e = STORE.one("SELECT status, review FROM entry WHERE id = ?", (i,))
        if e and e["status"] == "open":
            review = e["review"] if e["review"] != "unreviewed" else "accepted" if keep else "rejected"
            STORE.update_entry(i, status="done" if keep else "dismissed", review=review)


def reopen(cfg: dict, body: dict) -> None:
    ids = {int(body["entry"])} if body.get("entry") else set()
    for c in find_cards(cfg, str(body["card"])) if body.get("card") else []:
        threads.mark_read(STORE, c["key"], None, c["ping_items"])
        threads.mark_done(STORE, c["key"], None)
        ids.update(linked_entries(c))
    for i in ids:
        e = STORE.one("SELECT status, review FROM entry WHERE id = ?", (i,))
        if e and e["status"] in ("done", "dismissed"):
            STORE.update_entry(i, status="open", review="unreviewed" if e["review"] == "rejected" else e["review"])


def chat_url(cfg: dict, slug: str):
    for org in cfg.get("orgs", {}).values():
        for c in org.get("chats", []):
            if c.get("slug") == slug and c.get("thread"):
                t = c["thread"]
                return t if t.startswith("http") else f"https://www.facebook.com/messages/t/{t}"
    return None


def run_sync(every_chat: bool = False) -> None:
    with _sync_lock:
        cmd = [sys.executable, os.path.join(hub.ROOT, "hub.py"), "sync", "--all"]
        subprocess.run(cmd + ["--every-chat"] if every_chat else cmd, cwd=hub.ROOT)
        process.ingest(STORE, hub.load_config())


# --- HTTP ---------------------------------------------------------------------------------------


class Handler(config_ui.Handler):
    def do_GET(self):
        if self.path in ("/", "/app", "/bar"):
            with open(APP_UI, "rb") as f:
                return self.send(200, f.read(), "text/html")
        if self.path == "/config":
            with open(config_ui.UI, "rb") as f:
                return self.send(200, f.read(), "text/html")
        if self.path == "/api/app":
            return self.send(200, app_state())
        if self.path == "/api/ping":
            return self.send(200, {"ok": True, "app": "messenger-hub"})
        if self.path.startswith("/media/"):
            return self.send_media()
        return super().do_GET()

    def send_media(self):
        """A photo/video saved with a chat export: /media/<chat>/attachments/<file>. Only chats you added."""
        rel = urllib.parse.unquote(self.path[len("/media/") :].split("?")[0])
        slug, _, rest = rel.partition("/")
        if slug not in process.channels(hub.load_config()) or not rest.startswith("attachments/"):
            return self.send(404, {"error": "not found"})
        base = os.path.realpath(os.path.join(process.CHATS, slug, "attachments"))
        path = os.path.realpath(os.path.join(base, rest[len("attachments/") :]))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return self.send(404, {"error": "not found"})
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        partial = bool(rng and (rng.group(1) or rng.group(2)))
        if partial:  # videos seek with byte ranges
            if rng.group(1):
                start = int(rng.group(1))
                end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=86400")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except ConnectionError:  # broken pipe, reset or (Windows) aborted
                    return  # the player stopped reading
                left -= len(chunk)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send(403, {"error": "bad origin"})
        if not self.path.startswith("/api/app/"):
            return super().do_POST()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            action = self.path[len("/api/app/") :]
            cfg = hub.load_config()
            if action == "capture":
                if not str(body.get("text") or "").strip():
                    raise ValueError("empty note")
                process.capture(STORE, str(body["text"]), "app", cfg)
            elif action == "entry":
                fields = {k: body[k] for k in ("status", "review", "title", "due", "due_has_time", "org", "kind") if k in body}
                if fields.get("due"):
                    dt.datetime.fromisoformat(fields["due"])
                STORE.update_entry(int(body["id"]), **fields)
            elif action == "card-read":
                for c in find_cards(cfg, None if body.get("all") else str(body["key"])):
                    threads.mark_read(STORE, c["key"], None if body.get("unread") else c["last_ping"], c["ping_items"])
            elif action == "card-done":
                for c in find_cards(cfg, str(body["key"])):
                    threads.mark_done(STORE, c["key"], None if body.get("undo") else c["end"])
            elif action == "resolve":
                resolve(cfg, body)
            elif action == "reopen":
                reopen(cfg, body)
            elif action == "summarise":
                card = next(iter(find_cards(cfg, str(body["key"]))), None)
                if not card:
                    raise ValueError("that conversation is gone")
                threads.summarise(STORE, card, cfg.get("me") or "me")
            elif action == "open-chat":
                url = chat_url(cfg, str(body.get("channel") or ""))
                if not url:
                    raise ValueError("no Messenger link for this chat")
                threads.log_event(STORE, "open-source", str(body.get("key") or body.get("channel")))
                import webbrowser

                webbrowser.open(url)
            elif action == "open-link":
                url = str(body.get("url") or "")
                if not re.match(r"https?://", url):
                    raise ValueError("not a web link")
                import webbrowser

                webbrowser.open(url)
            elif action == "event":
                kind = str(body.get("kind") or "")
                if kind not in ("mentions-open", "mentions-clear", "home-answered"):
                    raise ValueError("unknown event")
                threads.log_event(STORE, kind, str(body.get("ref") or "")[:40])
            elif action == "profile":
                save_profile(body)
                process.rescan(STORE, hub.load_config())
            elif action == "settings":
                save_settings(body)
                WORKER.wake()
            elif action == "process":
                cfg = hub.load_config()
                (process.rescan if body.get("rescan") else process.ingest)(STORE, cfg)
            elif action == "sync":
                if not _sync_lock.locked():
                    WORKER.request_sync(body.get("full") is True)
            elif action == "sync-stop":
                WORKER.stop_sync()
            elif action == "live-resume":
                WORKER.resume()
            elif action == "live-audit":
                WORKER.request_audit()
            elif action == "telegram-token":
                BOT.set_token(str(body.get("token") or ""))
            elif action == "telegram-unpair":
                BOT.unpair()
            elif action == "telegram-test":
                if not BOT.send(botmod.compose_brief(STORE, cfg, "today", dt.datetime.now()) or "Messenger Hub is connected."):
                    raise ValueError(BOT.last_error or "not paired yet")
            elif action == "startup":
                set_startup(bool(body.get("on")))
            elif action == "desktop-shortcut":
                write_launcher(DESKTOP_FILE)
            elif action == "quit":
                self.send(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), WORKER.stop(), os._exit(0)), daemon=True).start()
                return
            else:
                return self.send(404, {"error": "unknown action"})
            self.send(200, app_state())
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            self.send(400, {"error": str(e)})


# --- background ---------------------------------------------------------------------------------


def processor_forever():
    while not _stop.is_set():
        try:
            cfg = hub.load_config()
            process.ingest(STORE, cfg)
            if settings.get(cfg)["summaries"] == "lite":
                cs = threads.cards(STORE, process.channels(cfg), process.profile(cfg))
                threads.summarise_pending(STORE, cs, cfg.get("me") or "me")
        except Exception as e:  # never crash the app over one bad file or a failed summary
            print(f"[process] {e}")
        minutes = settings.get(hub.load_config())["process_minutes"]
        _stop.wait(minutes * 60)


def running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "messenger-hub"
    except Exception:
        return False


def start_service(port: int):
    global STORE, BOT, WORKER
    import http.server

    STORE = Store()
    BOT = botmod.Bot(STORE, hub.load_config)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=processor_forever, daemon=True).start()
    BOT.start()
    WORKER = live.Worker(STORE, notice=BOT.notice, plain_sync=run_sync)
    WORKER.start()
    return server


class WindowApi:
    """Exposed to the page as window.pywebview.api (windows need the window's own process)."""

    def __init__(self, url: str, title: str = "Messenger Hub"):
        self._url, self._title = url, title
        self._window = self._bar = None  # underscore: pywebview must not walk the windows when exposing this object

    def main_window(self):
        import webview

        self._window = webview.create_window(self._title, self._url, width=1360, height=880, min_size=(340, 280), js_api=self)
        self._window.events.closed += self._main_closed
        return self._window

    def _main_closed(self):
        self._window = None

    def _bar_closed(self):
        self._bar = None

    def open_bar(self):
        """The floating bar: a frameless strip on top of everything, instead of the full window."""
        import webview

        if not self._bar:
            scr = webview.screens[0] if webview.screens else None
            x = max(0, ((scr.width if scr else 1920) - BAR_WIDTH) // 2)
            self._bar = webview.create_window(self._title + " bar", self._url + "bar", width=BAR_WIDTH, height=BAR_SHUT, x=x, y=10, min_size=(BAR_WIDTH, BAR_SHUT),
                                              frameless=True, easy_drag=False, on_top=True, resizable=False, js_api=self)
            self._bar.events.closed += self._bar_closed
        if self._window:
            self._window.minimize()

    def size_bar(self, opened: bool):
        if self._bar:
            self._bar.resize(BAR_WIDTH, BAR_OPEN if opened else BAR_SHUT)

    def close_bar(self):
        """Back to the full window."""
        if self._window:
            self._window.restore()
        else:
            self.main_window()
        if self._bar:
            self._bar.destroy()


def open_window(url: str) -> bool:
    try:
        import webview
    except ImportError:
        return False
    WindowApi(url).main_window()
    webview.start()
    return True


def main(no_window: bool = False, browser: bool = False, port: int = PORT) -> None:
    url = f"http://127.0.0.1:{port}/"
    if running(port):
        # The hub is already running in the background: just show it.
        if not (browser or no_window) and open_window(url):
            return
        if not no_window:
            import webbrowser

            webbrowser.open(url)
        return
    start_service(port)
    print(f"Messenger Hub running at {url}")
    if not no_window:
        if browser or not open_window(url):
            import webbrowser

            webbrowser.open(url)
        print("Window closed; the hub keeps running for briefs. Quit from Settings, or Ctrl+C.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _stop.set()
