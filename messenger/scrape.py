"""Scrape a Messenger conversation (text, replies, reactions, photos, files) into an agent-ready export.

    py -3.10 messenger/scrape.py <thread url or id> [--out DIR] [--me NAME] [--since YYYY-MM-DD]
                                 [--full] [--no-download] [--max-minutes N] [--debug]

Usually driven by hub.py, which syncs every chat of an organisation in one browser session.
Opens a real Chromium window with its own profile (.profile in the project root, git-ignored). The first
time, log in to Facebook in that window yourself (and enter your chat PIN if Messenger asks to
restore end-to-end encrypted history); the session is remembered for later runs. The script then
scrolls the chat up to load history, extracts every message and downloads its attachments.

Re-running is incremental. The export remembers its newest message (meta.json "last_exported"), so
the next run scrolls back only until the chat is older than that message, keeps only what came
after it, and downloads only the new attachments; nothing already exported is fetched twice.
meta.json "last_sync" says which message ids the latest run added. --full re-reads the whole
history. Ctrl+C (or a STOP file) stops scrolling early and still saves what was collected.

Output (default data/chats/<chat-name>/, git-ignored): see common.py.
"""

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROFILE = os.path.join(ROOT, ".profile")
CHATS = os.path.join(ROOT, "data", "chats")
STEP_WAIT = 1.6  # seconds between scrolls; slower is gentler on Facebook's rate limits
IDLE_ROUNDS = 8  # scrolls without new rows before we decide we've hit the start of the chat
LOGIN_TIMEOUT = 15 * 60
STOP_FILE = os.path.join(ROOT, "STOP")  # create this file to stop scrolling and save (see README)


def thread_url(arg: str) -> str:
    if arg.startswith("http"):
        return arg
    return f"https://www.facebook.com/messages/t/{arg}"


TIME_ONLY = re.compile(r"^\d{1,2}[:.]\d{2}(\s*[AaPp]\.?[Mm]\.?)?$")


def when_to_dt(when: str, now: dt.datetime):
    """Messenger labels today's messages with the time only ("8:15 PM") and older ones with a day
    ("Tuesday 11:55pm", "October 1, 2026, 9:10 AM"). A bare time is today, never the previous
    message's day: right after midnight that would date new messages a day early."""
    when = (when or "").replace(" ", " ").strip()
    if not when:
        return None
    parsed = common.parse_time_label(when, now)
    if parsed and TIME_ONLY.match(when):
        today = dt.datetime.combine(now.date(), parsed.time())
        return today if today <= now + dt.timedelta(minutes=1) else today - dt.timedelta(days=1)
    return parsed


def strip_label(text: str, r: dict) -> str:
    """Since Oct 2026 Messenger puts a sender label ("You sent", "Kim", "Edited") in front of the
    message text as its own block. Drop it: a lone "You sent"/"Edited", or a name line with the
    message underneath."""
    lines = (text or "").split("\n")
    names = {n for n in (r.get("sender"), r.get("heading"), r.get("avatar")) if n}
    names |= {f"{n} sent" for n in names}
    while lines:
        first = lines[0].strip()
        if first in ("You sent", "Edited") or (first in names and len(lines) > 1):
            lines.pop(0)
        else:
            break
    return "\n".join(lines).strip()


def to_messages(rows: list, me: str, now: dt.datetime) -> list:
    """Turn raw DOM items into messages: each message's accessible label gives its time and sender
    (date breaks are kept only as a fallback label), and avatars map short names ("Patrick") to full ones."""
    full = {}
    for r in rows:
        if r["kind"] == "message" and r.get("avatar") and r.get("sender") and r["sender"] != "You":
            full.setdefault(r["sender"], r["avatar"])
    out, label = [], None
    for r in rows:
        if r["kind"] == "time":
            label = r["label"]
            continue
        ts = when_to_dt(r.get("when"), now)
        sender = r.get("sender") or r.get("heading") or r.get("avatar") or None
        if sender == "You":
            sender = me
        elif sender:
            sender = full.get(sender, sender)
        # "Kim replied to Patrick" / "You replied to Kim" / "Patrick replied to themself"
        target = None
        hm = re.match(r"^(.+?) replied to (.+)$", r.get("reply_header") or "")
        if hm:
            who = hm.group(2).strip()
            if who == "you":
                target = me
            elif who in ("themself", "themselves", "himself", "herself", "yourself"):
                target = sender
            else:
                target = full.get(who, who)
        text = strip_label(r["text"], r)
        if not text and not (r["images"] or r["videos"] or r["audio"] or r["files"] or r["links"]):
            text = "(sticker, GIF or other content the scraper couldn't read)"
        out.append(
            {
                "kind": "message",
                "mid": r.get("mid") or None,
                "time_label": r.get("when") or label,
                "ts": ts.isoformat(timespec="minutes") if ts else None,
                "sender": sender,
                "text": text,
                "reply_quote": r["reply_quote"],
                "reply_header": r["reply_header"],
                "reply_target": target,
                "reactions": r["reactions"],
                "edited": r["edited"],
                "links": sorted({re.sub(r"[?&]fbclid=[^&#]*", "", u) for u in r["links"]}),
                "attachments": [{"type": "image", "url": i["url"], "id": i.get("attachment_id")} for i in r["images"]]
                + [{"type": "video", "url": v["url"]} for v in r["videos"]]
                + [{"type": "audio", "url": a["url"]} for a in r["audio"]]
                + [{"type": "file", "url": f["url"], "name": f["name"]} for f in r["files"]],
                "raw": r["raw"],
            }
        )
    return out


def download_attachments(context, messages: list, out_dir: str) -> None:
    folder = os.path.join(out_dir, "attachments")
    os.makedirs(folder, exist_ok=True)
    todo = [a for m in messages for a in m["attachments"] if re.match(r"https?:", a.get("url") or "")]
    for n, a in enumerate(todo, 1):
        base = hashlib.sha1(a["url"].split("?")[0].encode()).hexdigest()[:12]
        existing = [f for f in os.listdir(folder) if f.startswith(base)]
        if existing:
            a["path"] = f"attachments/{existing[0]}"
            continue
        try:
            resp = context.request.get(a["url"], timeout=60_000)
            if not resp.ok:
                print(f"  ! {a['type']} HTTP {resp.status}: {a['url'][:80]}")
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0]
            ext = mimetypes.guess_extension(ctype) or os.path.splitext(a["url"].split("?")[0])[1] or ".bin"
            if a["type"] == "file" and a.get("name"):
                stem = re.sub(r"[^\w.-]+", "_", a["name"])[:60]
                name = f"{base}_{stem}" if "." in stem else f"{base}_{stem}{ext}"
            else:
                name = base + (".jpg" if ext in (".jpe", ".jpeg") else ext)
            with open(os.path.join(folder, name), "wb") as f:
                f.write(resp.body())
            a["path"] = f"attachments/{name}"
            print(f"  [{n}/{len(todo)}] {a['type']} -> {a['path']}")
        except Exception as e:  # keep going; the URL stays in the export
            print(f"  ! {a['type']} failed: {e}")


def wait_for_chat(context, page, url: str):
    """Wait until the conversation is on screen. Survives the page navigating during login, and
    sends the browser back to the chat if Facebook lands on the home page after logging in."""
    deadline = time.time() + LOGIN_TIMEOUT
    told = sent_back = False
    while time.time() < deadline:
        page = context.pages[-1] if context.pages else page
        try:
            if page.evaluate("!!document.querySelector('[role=\"log\"] [role=\"article\"]')"):
                return page
        except Exception:
            pass  # the page navigated mid-check (login redirect); look again next tick
        logged_in = any(c["name"] == "c_user" for c in context.cookies())
        thread_id = url.rstrip("/").split("/")[-1]
        if logged_in and thread_id not in page.url and not sent_back:
            time.sleep(3)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:
                pass
            sent_back = True
        if not told:
            print("Waiting for the conversation to load. If you see a login page or a PIN prompt,")
            print("complete it in the browser window (and open the chat if it doesn't open by itself).")
            told = True
        time.sleep(2)
    sys.exit("Timed out waiting for the conversation.")


def save_rows(out_dir: str, rows: list, now: dt.datetime) -> None:
    """--debug: keep each run's raw rows in _rows/, so --rebuild can replay every sync in order."""
    folder = os.path.join(out_dir, "_rows")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, now.strftime("%Y%m%d-%H%M%S") + ".json"), "w", encoding="utf-8") as f:
        json.dump({"now": now.isoformat(timespec="seconds"), "rows": rows}, f, ensure_ascii=False, indent=1)


def saved_runs(out_dir: str) -> list:
    """(now, rows) for every saved run, oldest first. _rows.json is the single-file format of
    earlier versions; its file time stands in for when it was scraped."""
    runs = []
    legacy = os.path.join(out_dir, "_rows.json")
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as f:
            runs.append((dt.datetime.fromtimestamp(os.path.getmtime(legacy)), json.load(f)))
    folder = os.path.join(out_dir, "_rows")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            run = json.load(f)
        runs.append((dt.datetime.fromisoformat(run["now"]), run["rows"]))
    return sorted(runs, key=lambda r: r[0])


def rebuild(args) -> None:
    out_dir = args.out
    if not out_dir:
        sys.exit("--rebuild needs --out <export folder>")
    runs = saved_runs(out_dir)
    if not runs:
        sys.exit(f"No saved rows in {out_dir} (scrape with --debug to keep them)")
    with open(os.path.join(out_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    messages = []
    for now, rows in runs:
        fresh = to_messages(rows, args.me, now)
        if messages:
            messages += common.after_bookmark(fresh, messages)
        else:
            messages = [m for m in fresh if not args.since or not m["ts"] or m["ts"] >= args.since]
    folder = os.path.join(out_dir, "attachments")
    have = os.listdir(folder) if os.path.isdir(folder) else []
    for a in (a for m in messages for a in m["attachments"]):
        base = hashlib.sha1((a.get("url") or "").split("?")[0].encode()).hexdigest()[:12]
        hit = next((f for f in have if f.startswith(base)), None)
        if hit:
            a["path"] = f"attachments/{hit}"
    keep = {k: meta[k] for k in ("title", "source", "thread") if k in meta}
    common.write_export(out_dir, keep, messages)
    print(f"Rebuilt {len(messages)} messages -> {out_dir}")


def launch(p):
    """Open the persistent Chromium profile (headed: Facebook login and PIN prompts need a human)."""
    return p.chromium.launch_persistent_context(
        PROFILE,
        headless=False,
        viewport=None,
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
    )


def scrape_thread(
    context,
    thread: str,
    out_dir: str = None,
    me: str = "You",
    since: str = None,
    full: bool = False,
    no_download: bool = False,
    max_minutes: float = 60,
    debug: bool = False,
) -> dict:
    """Sync one conversation into out_dir (default data/chats/<title slug>). Returns a summary."""
    since_dt = dt.datetime.fromisoformat(since) if since else None
    now = dt.datetime.now()
    with open(os.path.join(HERE, "extract.js"), encoding="utf-8") as f:
        script = f.read()

    page = context.pages[0] if context.pages else context.new_page()
    page.goto(thread_url(thread), wait_until="domcontentloaded")
    page = wait_for_chat(context, page, thread_url(thread))
    time.sleep(3)
    page.evaluate(script)
    title = page.evaluate("window.__hsx.title()")
    out_dir = out_dir or os.path.join(CHATS, common.slugify(title))
    # Older exports may hold rows an earlier scrape captured twice: clean them first, so ids stay consistent.
    existing = [] if full else common.drop_repeats(common.load_messages(out_dir))
    known = {common.message_key(m) for m in existing}
    mark = common.bookmark(existing)
    print(f"Chat: {title}\nOutput: {out_dir}\nExisting messages: {len(existing)}")
    if mark:
        print(f"Last exported: #{mark['id']} {mark['ts']} {mark.get('sender')}; fetching only newer messages.")

    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    print(f"To stop early and keep what's collected: create {STOP_FILE} (or press Ctrl+C here).")
    started, idle, last_height = time.time(), 0, None
    try:
        for n in range(1, 100_000):
            res = page.evaluate("window.__hsx.step()")
            if not res.get("ok"):
                print("!", res.get("reason"))
                break
            grew = res["scrollHeight"] != last_height
            idle = 0 if (res["added"] or grew) else idle + 1
            last_height = res["scrollHeight"]
            if os.path.exists(STOP_FILE):
                os.remove(STOP_FILE)
                print("Stop requested; saving what was collected.")
                break
            if n % 5 == 0:
                head = to_messages(page.evaluate("window.__hsx.dump()")[:40], me, now)
                oldest = next((m["ts"] for m in head if m["ts"]), "?")
                print(f"  scroll {n}: {res['total']} items, back to {oldest}")
            if idle >= IDLE_ROUNDS:
                print("Reached the start of the conversation.")
                break
            if time.time() - started > max_minutes * 60:
                print("Time limit reached; saving what was collected.")
                break
            if (known or since_dt) and (n == 1 or n % 2 == 0):
                oldest = to_messages(page.evaluate("window.__hsx.dump()")[:60], me, now)
                msgs = [m for m in oldest if m["kind"] == "message"]
                # Everything after the bookmark is loaded once the top of the list is older than it.
                if mark and any(m["ts"] and m["ts"] < mark["ts"] for m in msgs):
                    print(f"Reached the last exported message ({mark['ts']}).")
                    break
                if not mark and known and sum(common.message_key(m) in known for m in msgs[:20]) >= 3:
                    print("Reached messages already in the export.")
                    break
                if since_dt and any(m["ts"] and m["ts"] < since_dt.isoformat() for m in msgs):
                    print(f"Reached {since}.")
                    break
            page.evaluate("window.__hsx.scrollUp()")
            time.sleep(STEP_WAIT)
    except KeyboardInterrupt:
        print("Stopped by user; saving what was collected.")

    page.evaluate("window.__hsx.step()")
    rows = page.evaluate("window.__hsx.dump()")
    os.makedirs(out_dir, exist_ok=True)
    if debug:
        save_rows(out_dir, rows, now)
    fresh = to_messages(rows, me, now)
    if existing:
        new_part = common.after_bookmark(fresh, existing)
    else:
        new_part = [m for m in fresh if not since_dt or not m["ts"] or m["ts"] >= since_dt.isoformat()]
    merged = common.drop_repeats(existing + new_part)
    new_part = merged[len(existing) :]
    added = len(new_part)
    if not no_download:
        print(f"Downloading attachments for {len(new_part)} new entries...")
        download_attachments(context, new_part, out_dir)

    meta = {
        "title": title,
        "source": f"web scrape of {thread_url(thread)}",
        "thread": thread,
        # Which ids this run added, so an agent can read just the new part of transcript.md.
        "last_sync": {
            "at": now.isoformat(timespec="seconds"),
            "new_messages": added,
            "first_new_id": len(existing) + 1 if added else None,
            "last_new_id": len(merged) if added else None,
        },
    }
    common.write_export(out_dir, meta, merged)
    print(f"Done: {len(merged)} entries ({added} new) -> {out_dir}")
    return {"title": title, "out_dir": out_dir, "added": added, "total": len(merged)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("thread", help="thread URL (facebook.com/messages/t/... or messenger.com/t/...) or id")
    ap.add_argument("--out", help="output folder (default data/chats/<chat-name>)")
    ap.add_argument("--me", default="You", help="name to use for your own messages")
    ap.add_argument("--since", help="stop scrolling once messages are older than this date (YYYY-MM-DD)")
    ap.add_argument("--full", action="store_true", help="re-read the whole history instead of only new messages")
    ap.add_argument("--no-download", action="store_true", help="record attachment URLs without downloading")
    ap.add_argument("--max-minutes", type=float, default=60, help="give up scrolling after this long")
    ap.add_argument("--debug", action="store_true", help="also save each run's raw DOM rows in _rows/")
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="no browser: rebuild the export in --out from its saved _rows/ (after fixing the parser)",
    )
    args = ap.parse_args()
    if args.rebuild:
        rebuild(args)
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch(p)
        res = scrape_thread(
            context,
            args.thread,
            out_dir=args.out,
            me=args.me,
            since=args.since,
            full=args.full,
            no_download=args.no_download,
            max_minutes=args.max_minutes,
            debug=args.debug,
        )
        context.close()
    print(f"Feed the agent {os.path.join(res['out_dir'], 'transcript.md')}")


if __name__ == "__main__":
    main()
