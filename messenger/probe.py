"""Record what the Messenger chat list does, to learn which signal shows a chat changed.

    py -3.10 messenger/probe.py --minutes 60
    py -3.10 messenger/probe.py --summary data/probe/<file>.jsonl

Opens the logged-in browser on the "new message" page and never clicks anything, so no chat is opened by
this script. It stores thread ids, hashes of row text, font weights, and the time and size of WebSocket
frames: no message bodies, names or frame contents. Output goes to data/probe/ (git-ignored).
Stops by itself on a login or checkpoint page, and on the STOP file.
"""

import argparse
import collections
import datetime as dt
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # noqa: E402

START_URL = "https://www.facebook.com/messages/new/"
OUT = os.path.join(scrape.ROOT, "data", "probe")
TICK = 5  # seconds between reads of the chat list


def wait_for_list(context, page):
    deadline = time.time() + scrape.LOGIN_TIMEOUT
    told = False
    with open(os.path.join(scrape.HERE, "watch.js"), encoding="utf-8") as f:
        script = f.read()
    while time.time() < deadline:
        if os.path.exists(scrape.STOP_FILE):
            sys.exit("Stopped while waiting for the chat list.")
        page = context.pages[-1]
        try:
            page.evaluate(script)
            if page.evaluate("window.__hsw.drain().rows.length") > 0:
                return page
        except Exception:
            pass  # navigating (login); look again next tick
        if not told:
            print("Waiting for your chat list. Log in or enter your PIN in the browser window if asked.")
            told = True
        time.sleep(2)
    sys.exit("Timed out waiting for the chat list.")


def record(minutes: float) -> str:
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
    if os.path.exists(scrape.STOP_FILE):
        os.remove(scrape.STOP_FILE)
    frames = collections.Counter()

    def on_socket(ws):
        host = urlparse(ws.url).hostname
        ws.on("framereceived", lambda payload: (frames.update({(host, "in"): 1, (host, "in_bytes"): len(payload)})))
        ws.on("framesent", lambda payload: (frames.update({(host, "out"): 1, (host, "out_bytes"): len(payload)})))

    with sync_playwright() as p, open(path, "w", encoding="utf-8") as f:
        context = scrape.launch(p)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("websocket", on_socket)
        page.goto(START_URL, wait_until="domcontentloaded")
        page = wait_for_list(context, page)

        def log(kind, **data):
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}) + "\n")
            f.flush()

        opened = "/messages/t/" in page.url or "/messages/e2ee/t/" in page.url
        log("start", path=urlparse(page.url).path, chat_open=opened)
        print(f"Recording to {path}. To stop early: create {scrape.STOP_FILE} (or press Ctrl+C here).")
        if opened:
            print("! Messenger opened a chat by itself; that chat may show as seen.")
        end = time.time() + minutes * 60
        last_rows = {}
        try:
            while time.time() < end:
                time.sleep(TICK)
                if os.path.exists(scrape.STOP_FILE):
                    os.remove(scrape.STOP_FILE)
                    log("stop", why="STOP file")
                    break
                why = scrape.blocked(page, context)
                if why:
                    log("stop", why=why)
                    print(f"! Facebook wants a human ({why}). Stopped; finish it in the browser window.")
                    break
                d = page.evaluate("window.__hsw.drain()")
                rows = {r["id"]: (r["sig"], r["weight"]) for r in d["rows"]}
                moved = {k: v for k, v in rows.items() if last_rows.get(k) != v}
                last_rows = rows
                log("tick", rows=len(rows), mutations=d["mutations"], changes=d["changes"], moved=moved,
                    frames={f"{h}|{k}": v for (h, k), v in frames.items()})
                frames.clear()
        except KeyboardInterrupt:
            log("stop", why="Ctrl+C")
        context.close()
    print(f"Saved {path}")
    return path


def summary(path: str) -> dict:
    ticks, changes, frames, stops = 0, [], collections.Counter(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["kind"] == "tick":
                ticks += 1
                changes += r["changes"]
                frames.update(r["frames"])
            elif r["kind"] == "stop":
                stops.append(r["why"])
    chats = collections.Counter(c["id"] for c in changes)
    return {"ticks": ticks, "row_changes": len(changes), "chats_changed": len(chats), "busiest": chats.most_common(5),
            "frames": dict(frames), "stopped": stops}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--summary", help="print what a recording holds")
    args = ap.parse_args()
    print(json.dumps(summary(args.summary), indent=1) if args.summary else record(args.minutes))
