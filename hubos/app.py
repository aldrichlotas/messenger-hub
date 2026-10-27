"""The Messenger Hub app: `py -3.10 hub.py app`.

One process runs the local server (127.0.0.1 only), the background processor (re-reads synced chats
every few minutes, no tokens) and the Telegram bot + scheduler. The window is a native desktop
window (pywebview) around the page; closing it leaves the hub running so briefs still arrive.
Running `hub.py app` again just opens another window onto the running hub.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import config_ui
import hub

from . import bot as botmod
from . import process, settings
from .store import Store

PORT = 8765
APP_UI = os.path.join(hub.ROOT, "ui", "app.html")
STARTUP_FILE = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Messenger Hub.vbs"
)

STORE = None
BOT = None
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


def app_state() -> dict:
    cfg = hub.load_config()
    d = process.dashboard(STORE)
    return {
        "dash": d,
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
        "syncing": _sync_lock.locked(),
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
    if "process_minutes" in body:
        cur["process_minutes"] = max(1, min(120, int(body["process_minutes"])))
    for b in cur["briefs"].values():
        dt.time.fromisoformat(b["at"])  # validate
    dt.time.fromisoformat(cur["quiet"]["start"]), dt.time.fromisoformat(cur["quiet"]["end"])
    cfg["settings"] = cur
    hub.save_config(cfg)


def run_sync() -> None:
    with _sync_lock:
        subprocess.run([sys.executable, os.path.join(hub.ROOT, "hub.py"), "sync", "--all"], cwd=hub.ROOT)
        process.ingest(STORE, hub.load_config())


# --- HTTP ---------------------------------------------------------------------------------------


class Handler(config_ui.Handler):
    def do_GET(self):
        if self.path in ("/", "/app"):
            with open(APP_UI, "rb") as f:
                return self.send(200, f.read(), "text/html")
        if self.path == "/config":
            with open(config_ui.UI, "rb") as f:
                return self.send(200, f.read(), "text/html")
        if self.path == "/api/app":
            return self.send(200, app_state())
        if self.path == "/api/ping":
            return self.send(200, {"ok": True, "app": "messenger-hub"})
        return super().do_GET()

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
            elif action == "mention-read":
                if body.get("all"):
                    STORE.run("UPDATE mention SET read = 1")
                else:
                    STORE.run("UPDATE mention SET read = ? WHERE id = ?", (0 if body.get("unread") else 1, int(body["id"])))
            elif action == "profile":
                save_profile(body)
                process.rescan(STORE, hub.load_config())
            elif action == "settings":
                save_settings(body)
            elif action == "process":
                cfg = hub.load_config()
                (process.rescan if body.get("rescan") else process.ingest)(STORE, cfg)
            elif action == "sync":
                if not _sync_lock.locked():
                    threading.Thread(target=run_sync, daemon=True).start()
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
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
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
            process.ingest(STORE, hub.load_config())
        except Exception as e:  # never crash the app over one bad file
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
    global STORE, BOT
    import http.server

    STORE = Store()
    BOT = botmod.Bot(STORE, hub.load_config)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=processor_forever, daemon=True).start()
    BOT.start()
    return server


class WindowApi:
    """Exposed to the page as window.pywebview.api (window modes need the window's own process)."""

    def __init__(self):
        self._window = None  # underscore: pywebview must not walk the window when exposing this object

    def set_mode(self, mode: str):
        import webview

        w = self._window
        scr = webview.screens[0] if webview.screens else None
        sw, sh = (scr.width, scr.height) if scr else (1920, 1080)
        if mode == "sidebar":
            w.on_top = True
            w.resize(430, sh - 80)
            w.move(sw - 440, 10)
        elif mode == "widget":
            w.on_top = True
            w.resize(380, 330)
            w.move(sw - 400, sh - 400)
        else:
            w.on_top = False
            w.resize(min(1360, sw - 80), min(880, sh - 80))
            w.move(max(0, (sw - min(1360, sw - 80)) // 2), 20)
        return mode


def open_window(url: str) -> bool:
    try:
        import webview
    except ImportError:
        return False
    api = WindowApi()
    api._window = webview.create_window("Messenger Hub", url, width=1360, height=880, min_size=(340, 280), js_api=api)
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
