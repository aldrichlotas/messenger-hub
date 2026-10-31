"""Local web page for editing orgs.json: `py -3.10 hub.py config` (http://127.0.0.1:8765).

Serves ui/index.html and a tiny JSON API. Listens on 127.0.0.1 only, so nothing outside this
computer can reach it. Nothing here fetches messages; "Refresh chat list" runs `hub.py discover`,
which only reads the chat names in your Messenger sidebar.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import webbrowser

import hub

UI = os.path.join(hub.ROOT, "ui", "index.html")
_discovering = threading.Lock()


def synced_info() -> dict:
    """thread id -> {messages, synced} for chats that have been exported."""
    out = {}
    if not os.path.isdir(hub.CHATS):
        return out
    for slug in os.listdir(hub.CHATS):
        path = os.path.join(hub.CHATS, slug, "meta.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        tid = hub.thread_id(meta.get("thread") or "")
        out[tid] = {
            "slug": slug,
            "messages": meta.get("message_count", 0),
            "synced": (meta.get("last_sync") or {}).get("at") or meta.get("exported_at"),
        }
    return out


def state() -> dict:
    chat_list = hub.load_chat_list()
    for c in chat_list["chats"]:
        c["label"] = hub.label(c)
    return {
        "config": hub.load_config(),
        "chatList": chat_list,
        "synced": synced_info(),
        "discovering": _discovering.locked(),
    }


def clean_config(cfg: dict) -> dict:
    """Validate what the page sends before it replaces orgs.json."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("orgs"), dict):
        raise ValueError("expected {me, orgs}")
    out = {"me": str(cfg.get("me") or "You").strip() or "You", "orgs": {}}
    # Sections this page doesn't edit (profile, settings...) pass through untouched.
    current = hub.load_config()
    for k, v in current.items():
        if k not in ("me", "orgs"):
            out[k] = cfg.get(k, v)
    for key, org in cfg["orgs"].items():
        key = hub.common.slugify(str(key))
        chats, seen = [], set()
        for c in org.get("chats") or []:
            tid = hub.thread_id(str(c.get("thread") or ""))
            if not tid or tid in seen:
                continue
            seen.add(tid)
            chats.append(
                {
                    "thread": str(c["thread"]).strip(),
                    "name": (str(c.get("name")).strip() or None) if c.get("name") else None,
                    "slug": c.get("slug") or None,
                    "enabled": bool(c.get("enabled", True)),
                }
            )
        entry = {"name": str(org.get("name") or key).strip(), "chats": chats}
        for field in ("my_title", "aliases", "committees", "above"):  # edited in the app's settings
            if org.get(field):
                entry[field] = org[field]
        for field in ("match", "context"):
            if str(org.get(field) or "").strip():
                entry[field] = str(org[field]).strip()
        outputs = [str(x).strip() for x in org.get("outputs") or [] if str(x).strip()]
        if outputs:
            entry["outputs"] = outputs
        out["orgs"][key] = entry
    return out


def run_discover(find_all: bool = False) -> None:
    with _discovering:
        cmd = [sys.executable, os.path.join(hub.ROOT, "hub.py"), "discover"]
        subprocess.run(cmd + ["--all-chats"] if find_all else cmd, cwd=hub.ROOT)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(UI, "rb") as f:
                self.send(200, f.read(), "text/html")
        elif self.path == "/api/state":
            self.send(200, state())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only accept requests from this page (blocks other sites posting to localhost).
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send(403, {"error": "bad origin"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/config":
                hub.save_config(clean_config(body))
                self.send(200, state())
            elif self.path == "/api/discover":
                if not _discovering.locked():
                    threading.Thread(target=run_discover, args=(body.get("all") is True,), daemon=True).start()
                self.send(200, {"started": True})
            elif self.path == "/api/discover-stop":
                if _discovering.locked():
                    with open(hub.STOP_FILE, "w", encoding="utf-8"):
                        pass
                self.send(200, {"stopping": True})
            else:
                self.send(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError) as e:
            self.send(400, {"error": str(e)})


def serve(port: int, open_browser: bool = True) -> None:
    for p in range(port, port + 20):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("No free port for the config page.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Config page: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
