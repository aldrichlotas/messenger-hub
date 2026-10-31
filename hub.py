"""Messenger Hub: sync your organisations' Messenger group chats and turn them into trackers,
roadmaps and briefs.

    py -3.10 hub.py app                                  the desktop app: today, deadlines, mentions, bot, settings
    py -3.10 hub.py process [--rescan [--since DATE]]    re-read synced chats with the rules (no tokens)
    py -3.10 hub.py threads --report                     check conversations + mention cards; writes sample cards
    py -3.10 hub.py config                               edit orgs and pick their group chats in your browser
    py -3.10 hub.py status                               what's configured, synced and analysed
    py -3.10 hub.py discover [--match ECG]               refresh the list of your chats (opens Messenger)
    py -3.10 hub.py org add ecg --name "ECG Organization" --match "ECG"
    py -3.10 hub.py chat add ecg <thread url> [--name "ECG Officers"]   (or: chat remove / enable / disable)
    py -3.10 hub.py sync ecg imes                        scrape new messages (one browser for all chats)
    py -3.10 hub.py analyze ecg                          Claude updates data/orgs/ecg/ (tracker, roadmap...)
    py -3.10 hub.py run --all                            sync + analyze every org

Config lives in orgs.json (git-ignored). Chats are stored once in data/chats/<slug>/ and can belong
to several orgs; each org's reports are in data/orgs/<org>/. See README.md.
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "messenger"))
import common  # noqa: E402

CONFIG = os.path.join(ROOT, "orgs.json")
DATA = os.path.join(ROOT, "data")
CHATS = os.path.join(DATA, "chats")
ORGS = os.path.join(DATA, "orgs")
CHAT_LIST = os.path.join(DATA, "chat-list.json")
PROMPT = os.path.join(ROOT, "prompts", "analyze.md")
DISCOVER_ROUNDS = 1500  # screens of chat list, at most; it normally stops at the end well before this
LIST_END_ROUNDS = 8  # screens at the bottom with nothing new: Messenger has no more chats to load
LIST_STUCK_ROUNDS = 25  # screens with nothing new anywhere: the list isn't loading
LIST_STATE = os.path.join(DATA, "list-state.json")  # each row's last signature, for skipping chats that haven't changed
SKIP_ROUNDS = 40  # screens of chat list a sync reads before it stops looking
SKIP_SAME_ROWS = 5  # unchanged rows in a row: the list is newest first, so nothing below them changed either
SKIP_IDLE_ROUNDS = 6  # screens at the bottom with no new rows: the list has ended
SKIP_STUCK_ROUNDS = 20  # screens anywhere with no new rows: it isn't loading. A few slow seconds are normal
NOTES_DAYS = 30  # how far back the first sync of your notes-to-self chat reads
SKIP_HOURS = 24  # a chat is opened at least this often, whatever the list says
BOLD = 600  # font weight that means unread (as in hubos/live.py)
STOP_FILE = os.path.join(ROOT, "STOP")  # create this file to stop a sync or refresh and keep what it has
LIST_QUIET_DAYS = 365  # a normal refresh stops at chats this quiet; `discover --all-chats` keeps going
LIST_QUIET_ROWS = 5  # this many quiet rows in a row: one odd row isn't the end of the active chats
SPAN_DAYS = {"h": 0, "hr": 0, "d": 1, "day": 1, "w": 7, "wk": 7, "week": 7, "mo": 30, "mon": 30, "month": 30, "y": 365, "yr": 365, "year": 365}
AGE_SPAN = re.compile(r"^(\d{1,4})\s*(mo|mon|months?|y|yrs?|years?|w|wks?|weeks?|d|days?|h|hrs?|hours?)(?:\s+ago)?$", re.I)
AGE_YEAR = re.compile(r"^[A-Za-z]{3,9}\.? \d{1,2}, (\d{4})$")
CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep"
UI_PORT = 8765
# Sidebar lines that aren't the chat's name.
STATUS_LINE = re.compile(r"^(active now|active \d+\s*\w+ ago|unread messages?:?|·|\d+\s*[mhdwy])$", re.I)


# --- config -------------------------------------------------------------------------------------


def load_config() -> dict:
    if not os.path.exists(CONFIG):
        return {"me": "You", "orgs": {}}
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("me", "You")
    cfg.setdefault("orgs", {})
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def thread_id(url: str) -> str:
    m = re.search(r"/t/([^/?#]+)", url or "")
    return m.group(1) if m else (url or "").strip("/")


def pick_orgs(cfg: dict, names: list, all_: bool) -> list:
    if all_ or not names:
        if not cfg["orgs"]:
            sys.exit("No organisations yet. Add one: py -3.10 hub.py org add <key> --name <name> --match <text>")
        if not all_ and len(cfg["orgs"]) > 1:
            sys.exit(f"Name an org ({', '.join(cfg['orgs'])}) or pass --all.")
        return list(cfg["orgs"])
    for n in names:
        if n not in cfg["orgs"]:
            sys.exit(f"Unknown org '{n}'. Known: {', '.join(cfg['orgs']) or 'none'}")
    return names


def add_chat(org: dict, url: str, name: str = None) -> bool:
    tid = thread_id(url)
    if any(thread_id(c["thread"]) == tid for c in org["chats"]):
        return False
    org["chats"].append({"thread": url, "name": name, "slug": None, "enabled": True})
    return True


def active(chats: list) -> list:
    return [c for c in chats if c.get("enabled", True)]


def name_chats(raw: list) -> list:
    """Turn the sidebar's raw text lines into {id, url, name, community, preview}. Chats inside a
    Messenger community show the community's name first; a name shared by several links is a
    community, so the chat's own name is the next line."""
    rows = []
    for c in raw:
        lines = c.get("lines") or [c.get("name", "")] + [x for x in (c.get("preview") or "").split(" · ") if x]
        rows.append((c, [x for x in lines if x and not STATUS_LINE.match(x.strip())]))
    counts = collections.Counter(lines[0] for _, lines in rows if lines)
    out = []
    for c, lines in rows:
        if not lines:
            continue
        name, community, rest = lines[0], None, lines[1:]
        if counts[name] > 1 and rest:
            community, name, rest = name, rest[0], rest[1:]
        out.append(
            {"id": c["id"], "url": c["url"], "name": name, "community": community, "preview": " · ".join(rest[:2])}
        )
    return out


def load_chat_list() -> dict:
    if not os.path.exists(CHAT_LIST):
        return {"at": None, "chats": []}
    with open(CHAT_LIST, encoding="utf-8") as f:
        data = json.load(f)
    data["chats"] = name_chats(data["chats"])
    return data


def label(c: dict) -> str:
    return f"{c['community']} › {c['name']}" if c.get("community") else c["name"]


def clear_stop() -> None:
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)


def chat_dir(chat: dict):
    return os.path.join(CHATS, chat["slug"]) if chat.get("slug") else None


# --- browser commands ---------------------------------------------------------------------------


def age_days(lines: list, today: dt.date = None):
    """How long ago a chat list row was active, from its time label ("3d", "2y", "· 52w", "Oct 12, 2024").
    None when the row has no label we can read: it then neither ends a refresh nor keeps it going."""
    today = today or dt.date.today()
    for line in reversed(lines):
        label = line.rsplit("·", 1)[-1].strip()
        if "·" not in line and len(line.split()) > 3:
            continue  # a preview, not a label
        m = AGE_SPAN.match(label)
        if m:
            unit = m.group(2).lower().rstrip("s")
            return int(m.group(1)) * SPAN_DAYS[unit]
        m = AGE_YEAR.match(label)
        if m:
            return (today - dt.date(int(m.group(1)), 12, 31)).days
    return None


def quiet_rows(chats: list, today: dt.date = None) -> int:
    """How many of the last rows read, in a row, are past LIST_QUIET_DAYS. Rows without a label don't count either way."""
    n = 0
    for c in reversed(chats):
        age = age_days(c.get("lines") or [], today)
        if age is None:
            continue
        if age < LIST_QUIET_DAYS:
            break
        n += 1
    return n


def read_chat_list(page, rounds: int = DISCOVER_ROUNDS, pause: float = 1.2, find_all: bool = False) -> list:
    """Walk the chat list a screen at a time, with discover.js already on the page. It's newest first,
    so it stops once it reaches chats quiet for a year, or at the end with find_all. Closing the
    browser window part-way keeps what was read up to then."""
    chats = []
    try:
        return walk_chat_list(page, rounds, pause, find_all, chats)
    except Exception as e:  # Playwright's "target closed": the page is gone, so use our own copy
        print(f"The browser went away ({type(e).__name__}), keeping the {len(chats)} chats read so far.")
        return chats


def walk_chat_list(page, rounds: int, pause: float, find_all: bool, chats: list) -> list:
    last, idle = 0, 0
    for n in range(1, rounds + 1):
        count = page.evaluate("window.__hsd.collect()")
        chats[:] = page.evaluate("window.__hsd.list()")
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
            print(f"Stopped at your request, keeping the {count} chats read so far.")
            break
        idle = 0 if count > last else idle + 1
        if count > last and n % 10 == 0:
            print(f"  {count} chats so far...")
        last = count
        if not find_all and quiet_rows(chats) >= LIST_QUIET_ROWS:
            print(f"Stopped at chats quiet for over a year. To go further: `discover --all-chats`, or Find all chats on the config page.")
            break
        at = page.evaluate("window.__hsd.step()")
        if (at["end"] and idle >= LIST_END_ROUNDS) or idle >= LIST_STUCK_ROUNDS:
            break
        time.sleep(pause)
    return page.evaluate("window.__hsd.list()")


def merge_chat_lists(found: list, known: list) -> list:
    """What this refresh found, then what earlier ones found and this one didn't: a refresh cut short
    can't shrink the list, and a chat you once saw stays pickable."""
    ids = {c["id"] for c in found}
    return found + [c for c in known if c["id"] not in ids]


def discover(cfg: dict, args) -> None:
    from playwright.sync_api import sync_playwright

    import scrape

    with open(os.path.join(ROOT, "messenger", "discover.js"), encoding="utf-8") as f:
        script = f.read()
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    found = None
    with sync_playwright() as p:
        context = scrape.launch(p)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com/messages/new/", wait_until="domcontentloaded")  # opens no chat
        deadline = time.time() + scrape.LOGIN_TIMEOUT
        told = False
        while time.time() < deadline:
            if os.path.exists(STOP_FILE):  # stopped before the list even showed: nothing to keep
                os.remove(STOP_FILE)
                found = []
                break
            page = context.pages[-1]
            try:
                page.evaluate(script)
                if page.evaluate("window.__hsd.collect()") > 0:
                    break
            except Exception:
                pass  # navigating (login); try again
            if not told:
                print("Waiting for your chat list. Log in / enter your PIN in the browser window if asked.")
                told = True
            time.sleep(2)
        if found is None:
            time.sleep(2)
            found = read_chat_list(page, args.rounds, find_all=args.all_chats)
        try:
            context.close()
        except Exception:  # you closed the window yourself
            pass

    known = []
    if os.path.exists(CHAT_LIST):
        with open(CHAT_LIST, encoding="utf-8") as f:
            known = json.load(f)["chats"]
    chats = merge_chat_lists(found, known)
    os.makedirs(DATA, exist_ok=True)
    with open(CHAT_LIST, "w", encoding="utf-8") as f:
        json.dump({"at": dt.datetime.now().isoformat(timespec="seconds"), "chats": chats}, f, ensure_ascii=False, indent=1)
    print(f"Found {len(found)} chats, {len(chats) - len(found)} more kept from earlier (saved to {os.path.relpath(CHAT_LIST, ROOT)}).")

    # Only lists them. Choosing which chats belong to an org is up to you: `hub.py config`,
    # `chat add`, or `discover --add` to add every chat matching an org's --match.
    flt = re.compile(args.match, re.I) if args.match else None
    added = 0
    for c in name_chats(chats):
        owners = [k for k, o in cfg["orgs"].items() if o.get("match") and re.search(o["match"], label(c), re.I)]
        if flt and not flt.search(label(c)):
            continue
        tag = f"  -> {', '.join(owners)}" if owners else ""
        print(f"  {label(c)}  {c['url']}{tag}")
        if args.add:
            for k in owners:
                if add_chat(cfg["orgs"][k], c["url"], label(c)):
                    added += 1
                    print(f"     added to {k}")
    if args.add:
        save_config(cfg)
        print(f"{added} chat(s) added.")
    else:
        print("Nothing was added. Pick chats with `py -3.10 hub.py config`.")


def load_list_state() -> dict:
    state = {"rows": {}, "opened": {}, "tries": {}}
    if os.path.exists(LIST_STATE):
        with open(LIST_STATE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_list_state(state: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(LIST_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0)


def row_moved(row: dict, known: dict) -> bool:
    """Has this chat changed since its row was last noted? Reading a chat elsewhere (bold to plain) doesn't count."""
    return not known or known["sig"] != row["sig"] or (row["weight"] >= BOLD > known["weight"])


def read_rows(page, want: set, known: dict, rounds: int = SKIP_ROUNDS, pause: float = 0.6) -> dict:
    """The chat list from the top, with watch.js and discover.js on the page: {id: row}. Newest first, so it
    can stop at a run of unchanged rows, or once it has seen every chat it wants."""
    rows, same, idle = {}, 0, 0
    for _ in range(rounds):
        fresh = [r for r in page.evaluate("window.__hsw.drain().rows") if r["id"] not in rows]
        for r in fresh:
            rows[r["id"]] = r
            same = 0 if row_moved(r, known.get(r["id"])) else same + 1
        idle = 0 if fresh else idle + 1
        if same >= SKIP_SAME_ROWS or want <= set(rows) or idle >= SKIP_STUCK_ROUNDS or os.path.exists(STOP_FILE):
            break
        at = page.evaluate("window.__hsd.step()")
        if at["end"] and idle >= SKIP_IDLE_ROUNDS:
            break
        time.sleep(pause)
    return rows


def pick_changed(chats: list, rows: dict, state: dict, now: dt.datetime, every_chat: bool = False) -> list:
    """Which chats to open. Left out: a chat opened recently whose row is unchanged, or that sits below the
    unchanged rows, even if its row has never been read: the list is newest first, so a new message
    would have brought it up. Anything else in doubt gets opened."""
    if every_chat or not rows:
        return chats
    oldest = (now - dt.timedelta(hours=SKIP_HOURS)).isoformat(timespec="seconds")
    out = []
    for c in chats:
        tid = thread_id(c["thread"])
        if state["opened"].get(tid, "") < oldest or (tid in rows and row_moved(rows[tid], state["rows"].get(tid))):
            out.append(c)
    return out


def note_opened(state: dict, tid: str, row: dict, added: int, now: dt.datetime) -> None:
    """After a chat synced. A changed row with nothing new may mean the page was behind the list, so that
    chat gets one more look next sync before its row counts as seen. A reaction also lands here: it costs one extra open."""
    state["opened"][tid] = now.isoformat(timespec="seconds")
    if not row:
        return
    moved = row_moved(row, state["rows"].get(tid)) and tid in state["rows"]
    if moved and not added and state["tries"].get(tid) != row["sig"]:
        state["tries"][tid] = row["sig"]
        return
    state["tries"].pop(tid, None)
    state["rows"][tid] = {"sig": row["sig"], "weight": row["weight"]}


def note_other_rows(state: dict, rows: dict, chats: list) -> None:
    """Rows of chats this sync doesn't cover still mark where unchanged rows begin next time."""
    mine = {thread_id(c["thread"]) for c in chats}
    for tid, r in rows.items():
        if tid not in mine:
            state["rows"][tid] = {"sig": r["sig"], "weight": r["weight"]}


def look_at_list(context, chats: list, state: dict) -> dict:
    """Read the chat list without opening a chat. Empty on any trouble, which makes the sync open every chat."""
    import probe

    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(probe.START_URL, wait_until="domcontentloaded")
        page = probe.wait_for_list(context, page)
        with open(os.path.join(ROOT, "messenger", "discover.js"), encoding="utf-8") as f:
            page.evaluate(f.read())
        return read_rows(page, {thread_id(c["thread"]) for c in chats}, state["rows"])
    except (Exception, SystemExit) as e:
        print(f"! Couldn't read the chat list ({type(e).__name__}): opening every chat.")
        return {}


def sync_targets(cfg: dict, keys: list) -> list:
    """The chats a sync visits: each enabled chat once, plus your notes-to-self thread."""
    chats, seen = [], set()
    for k in keys:
        for c in active(cfg["orgs"][k]["chats"]):
            tid = thread_id(c["thread"])
            if tid not in seen:
                seen.add(tid)
                chats.append(c)
    me_chat = (cfg.get("profile") or {}).get("self_chat")
    if me_chat and thread_id(me_chat) not in seen:
        # Your notes-to-self thread: the capture inbox, synced with every run. Every message there
        # becomes a note, so its first sync reads back NOTES_DAYS, not to the start of the chat.
        slug = (cfg.get("profile") or {}).get("self_slug")
        since = None if slug else (dt.date.today() - dt.timedelta(days=NOTES_DAYS)).isoformat()
        chats.append({"thread": me_chat, "name": "Notes to self", "slug": slug, "since": since})
    return chats


def record_chat(cfg: dict, c: dict, res: dict) -> None:
    """Remember a synced chat's folder and title, on the file as it is now: a sync's cfg is
    minutes old, and saving that would undo any setting changed in the app meanwhile."""
    slug = os.path.basename(res["out_dir"])
    fresh = load_config()
    for one in (cfg, fresh):
        me_chat = (one.get("profile") or {}).get("self_chat")
        if me_chat and thread_id(c["thread"]) == thread_id(me_chat):
            one.setdefault("profile", {})["self_slug"] = slug
        # The same chat may be listed in several orgs: record its slug everywhere.
        for o in one["orgs"].values():
            for oc in o["chats"]:
                if thread_id(oc["thread"]) == thread_id(c["thread"]):
                    oc["slug"] = oc.get("slug") or slug
                    oc["name"] = oc.get("name") or res["title"]
    save_config(fresh)


def sync(cfg: dict, keys: list, args) -> None:
    from playwright.sync_api import sync_playwright

    import scrape

    chats = sync_targets(cfg, keys)
    if not chats:
        sys.exit("These orgs have no enabled chats: pick some with `hub.py config` or `chat add`.")
    state = load_list_state()
    with sync_playwright() as p:
        context = scrape.launch(p)
        rows = look_at_list(context, chats, state)
        now = dt.datetime.now()
        todo = pick_changed(chats, rows, state, now, args.every_chat)
        if rows:
            print(f"Chat list read: opening {len(todo)} of {len(chats)} chats.")
        note_other_rows(state, rows, chats)
        for n, c in enumerate(todo, 1):
            if os.path.exists(STOP_FILE):
                print(f"Stopped at your request. Chats not opened: {len(todo) - n + 1}; the next sync gets them.")
                break
            print(f"\n=== [{n}/{len(todo)}] {c.get('name') or c['thread']}")
            try:
                res = scrape.scrape_thread(
                    context,
                    c["thread"],
                    out_dir=chat_dir(c),
                    me=cfg["me"],
                    since=args.since or c.get("since"),
                    no_download=args.no_download,
                    max_minutes=args.max_minutes,
                    debug=args.debug,
                )
            except Exception as e:  # one broken chat shouldn't stop the rest
                print(f"! failed: {e}")
                continue
            if res.get("dropped"):  # counts as not opened, so the next sync reads it again
                continue
            record_chat(cfg, c, res)
            tid = thread_id(c["thread"])
            note_opened(state, tid, rows.get(tid), res["added"], dt.datetime.now())
            save_list_state(state)
        save_list_state(state)
        context.close()
    clear_stop()


# --- analysis -----------------------------------------------------------------------------------


def org_paths(key: str) -> dict:
    d = os.path.join(ORGS, key)
    return {
        "dir": d,
        "state": os.path.join(d, "state.json"),
        "inbox": os.path.join(d, "inbox.md"),
        "prompt": os.path.join(d, "_prompt.md"),
    }


def load_state(key: str) -> dict:
    path = org_paths(key)["state"]
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"chats": {}, "runs": []}


def save_state(key: str, state: dict) -> None:
    with open(org_paths(key)["state"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def pending(cfg: dict, key: str, state: dict) -> list:
    """(chat, messages, new messages) for every synced chat of the org."""
    out = []
    for c in active(cfg["orgs"][key]["chats"]):
        d = chat_dir(c)
        if not d or not os.path.exists(os.path.join(d, "messages.jsonl")):
            continue
        msgs = common.load_messages(d)
        done = state["chats"].get(c["slug"], 0)
        out.append((c, msgs, [m for m in msgs if m["id"] > done]))
    return out


def build_inbox(cfg: dict, key: str, state: dict, todo: list) -> tuple:
    org = cfg["orgs"][key]
    lines = [
        f"# New messages for {org['name']}",
        "",
        f"Built {dt.datetime.now().isoformat(timespec='minutes')}. Message refs are `<chat>#<id>`;"
        " the full history of each chat is in `data/chats/<chat>/transcript.md` (search `[#<id>`).",
        "",
    ]
    total = 0
    for c, msgs, new in todo:
        if not new:
            continue
        total += len(new)
        by_id = {m["id"]: m for m in msgs}
        first_time = state["chats"].get(c["slug"], 0) == 0
        lines += [
            f"## {c.get('name') or c['slug']} (`{c['slug']}`)",
            "",
            f"{'Whole history (first analysis)' if first_time else 'New since last analysis'}:"
            f" messages #{new[0]['id']}–#{new[-1]['id']} of {len(msgs)}."
            f" Attachments are under `data/chats/{c['slug']}/`.",
            "",
        ]
        day = None
        for m in new:
            d = (m.get("ts") or "")[:10]
            if d and d != day:
                day = d
                lines += ["", f"### {dt.date.fromisoformat(d).strftime('%a %b %d, %Y')}", ""]
            body = common.format_message(m, by_id, prefix=c["slug"])
            lines += [x.replace("attachments/", f"data/chats/{c['slug']}/attachments/", 1) for x in body]
        lines.append("")
    return "\n".join(lines) + "\n", total


def fill_prompt(cfg: dict, key: str) -> str:
    org = cfg["orgs"][key]
    p = org_paths(key)
    with open(PROMPT, encoding="utf-8") as f:
        text = f.read()
    rel = lambda path: os.path.relpath(path, ROOT).replace("\\", "/")  # noqa: E731
    chats = "\n".join(f"- `{c['slug']}`: {c.get('name') or c['slug']}" for c in org["chats"] if c.get("slug"))
    values = {
        "ORG_NAME": org["name"],
        "ORG_DIR": rel(p["dir"]),
        "INBOX": rel(p["inbox"]),
        "ME": cfg["me"],
        "TODAY": dt.date.today().strftime("%A, %B %d, %Y"),
        "CONTEXT": org.get("context") or "(none given)",
        "OUTPUTS": ", ".join(org.get("outputs") or []) or "(none: choose what fits)",
        "CHATS": chats or "(none)",
    }
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def mark_done(key: str, state: dict, todo: list, total: int) -> None:
    for c, msgs, _ in todo:
        if msgs:
            state["chats"][c["slug"]] = msgs[-1]["id"]
    state["runs"].append({"at": dt.datetime.now().isoformat(timespec="seconds"), "messages": total})
    state["runs"] = state["runs"][-50:]
    save_state(key, state)
    for f in (org_paths(key)["inbox"], org_paths(key)["prompt"]):
        if os.path.exists(f):
            os.remove(f)


def analyze(cfg: dict, key: str, args) -> bool:
    org = cfg["orgs"][key]
    p = org_paths(key)
    os.makedirs(p["dir"], exist_ok=True)
    state = load_state(key)
    if args.reset:
        state = {"chats": {}, "runs": state.get("runs", [])}
    todo = pending(cfg, key, state)

    if args.mark_done:
        total = sum(len(n) for _, _, n in todo)
        mark_done(key, state, todo, total)
        print(f"[{key}] marked {total} messages as analysed.")
        return True

    inbox, total = build_inbox(cfg, key, state, todo)
    if not todo:
        print(f"[{key}] no synced chats yet; run `hub.py sync {key}` first.")
        return False
    if total == 0 and not args.force:
        print(f"[{key}] nothing new since the last analysis.")
        return True
    with open(p["inbox"], "w", encoding="utf-8") as f:
        f.write(inbox)
    prompt = fill_prompt(cfg, key)
    with open(p["prompt"], "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"[{key}] {total} new messages -> {os.path.relpath(p['inbox'], ROOT)}")
    if args.prepare:
        print(f"Prepared. Follow {os.path.relpath(p['prompt'], ROOT)}, then: py -3.10 hub.py analyze {key} --mark-done")
        return True

    claude = shutil.which("claude")
    if not claude:
        sys.exit("The `claude` CLI isn't on PATH. Install Claude Code, or use --prepare and run /digest in it.")
    cmd = [claude, "-p", "--permission-mode", "acceptEdits", "--allowedTools", CLAUDE_TOOLS]
    if args.model:
        cmd += ["--model", args.model]
    print(f"[{key}] Claude is updating {os.path.relpath(p['dir'], ROOT)} (this can take a few minutes)...")
    started = time.time()
    res = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", cwd=ROOT, capture_output=True)
    print(res.stdout.strip())
    readme = os.path.join(p["dir"], "README.md")
    if res.returncode != 0 or not os.path.exists(readme) or os.path.getmtime(readme) < started:
        print(res.stderr.strip())
        print(f"[{key}] analysis didn't finish (exit {res.returncode}); the inbox is kept for the next try.")
        return False
    mark_done(key, state, todo, total)
    print(f"[{key}] done: open {os.path.relpath(readme, ROOT)}")
    return True


# --- status -------------------------------------------------------------------------------------


def status(cfg: dict) -> None:
    print(f"Config: {os.path.relpath(CONFIG, ROOT)} · you are '{cfg['me']}'")
    if not cfg["orgs"]:
        print("No organisations yet.")
        return
    for key, org in cfg["orgs"].items():
        state = load_state(key)
        last = state["runs"][-1]["at"] if state.get("runs") else "never"
        print(f"\n{key}: {org['name']}  (match: {org.get('match') or '-'} · last analysed: {last})")
        if not org["chats"]:
            print("  no chats yet")
        for c in org["chats"]:
            d = chat_dir(c)
            meta = {}
            if d and os.path.exists(os.path.join(d, "meta.json")):
                with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            count = meta.get("message_count", 0)
            new = count - state["chats"].get(c.get("slug"), 0) if meta else 0
            synced = (meta.get("last_sync") or {}).get("at") or meta.get("exported_at") or "never"
            off = "" if c.get("enabled", True) else " [disabled]"
            print(f"  - {c.get('name') or c['thread']}{off}: {count} msgs, synced {synced}, {new} not analysed")


# --- CLI ----------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show orgs, chats and what's waiting to be analysed")

    o = sub.add_parser("org", help="add, edit or remove an organisation")
    o.add_argument("action", choices=["add", "edit", "remove"])
    o.add_argument("key", help="short id, e.g. ecg")
    o.add_argument("--name", help="display name, e.g. 'ECG Organization'")
    o.add_argument("--match", help="regex suggesting which chats belong here (config highlights them)")
    o.add_argument("--context", help="what the org is and your role in it; steers the analysis")
    o.add_argument("--outputs", help="comma list of reports to always keep, e.g. 'roadmap,event-plan,budget'")

    a = sub.add_parser("app", help="open the Messenger Hub app (starts the hub if it isn't running)")
    a.add_argument("--no-window", action="store_true", help="run in the background only (briefs, bot)")
    a.add_argument("--browser", action="store_true", help="use your web browser instead of a desktop window")

    pr = sub.add_parser("process", help="read synced chats into the local store with the rules (no tokens)")
    pr.add_argument("--rescan", action="store_true", help="re-run the rules over everything")
    pr.add_argument("--since", help="with --rescan: keep findings from this date (YYYY-MM-DD) open for review")

    va = sub.add_parser("vault", help="rebuild the Obsidian notes from the store (no tokens, no browser)")
    va.add_argument("--out", help="the generated folder (default: Hub in your Obsidian vault)")

    th = sub.add_parser("threads", help="check how chats split into conversations and the mention cards (no tokens)")
    th.add_argument("--report", action="store_true", help="print the checks and write 10 sample cards (default)")
    th.add_argument("--samples", type=int, default=10)
    th.add_argument("--review", action="store_true", help="write what Home hides to data/reports/answered-review.csv for you to label")
    th.add_argument("--score", action="store_true", help="how often hiding was right, from your labels in that file")
    th.add_argument("--org", help="with --review/--score/--sample: one org, in its own files")
    th.add_argument("--sample", action="store_true", help="with --org: 50 random messages naming you, for you to label for_me y/n")

    cf = sub.add_parser("config", help="edit orgs and their chats in a local web page")
    cf.add_argument("--no-open", action="store_true", help="don't open the page in your browser")

    c = sub.add_parser("chat", help="add, remove, enable or disable a chat")
    c.add_argument("action", choices=["add", "remove", "enable", "disable"])
    c.add_argument("org")
    c.add_argument("thread", help="https://www.facebook.com/messages/t/<id> (or e2ee/t/<id>) or the id")
    c.add_argument("--name")

    lv = sub.add_parser("live", help="live sync: delay, chats opened per day, checkpoints (no browser)")
    lv.add_argument("--days", type=int, default=7, help="how far back the report goes")
    lv.add_argument("--audit", action="store_true", help="ask the running app to compare the last 24 hours against a separate full read")

    d = sub.add_parser("discover", help="list the chats in your Messenger sidebar (opens the browser)")
    d.add_argument("--match", help="only show chats whose name matches this regex")
    d.add_argument("--add", action="store_true", help="also add every chat matching an org's --match to it")
    d.add_argument("--rounds", type=int, default=DISCOVER_ROUNDS, help="how far to scroll the chat list")
    d.add_argument("--all-chats", action="store_true", help="keep going past chats quiet for over a year, to the end of the list")

    for name, hlp in (("sync", "scrape new messages"), ("analyze", "update reports"), ("run", "sync then analyze")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("orgs", nargs="*")
        s.add_argument("--all", action="store_true")
        if name in ("sync", "run"):
            s.add_argument("--since", help="how far back a chat's first sync goes (YYYY-MM-DD)")
            s.add_argument("--no-download", action="store_true")
            s.add_argument("--max-minutes", type=float, default=30, help="per chat")
            s.add_argument("--every-chat", action="store_true", help="open every chat, not just those the chat list shows as changed")
            s.add_argument("--debug", action="store_true")
        if name in ("analyze", "run"):
            s.add_argument("--model", help="Claude model for the analysis (default: your Claude Code default)")
            s.add_argument("--force", action="store_true", help="re-run even with no new messages")
            s.add_argument("--reset", action="store_true", help="re-read every chat from the start")
            s.add_argument("--prepare", action="store_true", help="only build the inbox and prompt (for /digest)")
            s.add_argument("--mark-done", action="store_true", help="record the pending messages as analysed")

    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "status":
        status(cfg)
    elif args.cmd == "org":
        orgs = cfg["orgs"]
        if args.action == "remove":
            orgs.pop(args.key, None)
        else:
            if args.action == "edit" and args.key not in orgs:
                sys.exit(f"Unknown org '{args.key}'")
            org = orgs.setdefault(args.key, {"name": args.name or args.key, "chats": []})
            for field in ("name", "match", "context"):
                if getattr(args, field) is not None:
                    org[field] = getattr(args, field)
            if args.outputs is not None:
                org["outputs"] = [x.strip() for x in args.outputs.split(",") if x.strip()]
        save_config(cfg)
        status(cfg)
    elif args.cmd == "chat":
        if args.org not in cfg["orgs"]:
            sys.exit(f"Unknown org '{args.org}'")
        org = cfg["orgs"][args.org]
        tid = thread_id(args.thread)
        mine = [x for x in org["chats"] if thread_id(x["thread"]) == tid or x.get("slug") == args.thread]
        if args.action == "add":
            print("added" if add_chat(org, args.thread, args.name) else "already in this org")
        elif not mine:
            sys.exit("That chat isn't in this org.")
        elif args.action == "remove":
            org["chats"] = [x for x in org["chats"] if x not in mine]
        else:
            for x in mine:
                x["enabled"] = args.action == "enable"
        save_config(cfg)
        status(cfg)
    elif args.cmd == "app":
        from hubos import app

        app.main(no_window=args.no_window, browser=args.browser)
    elif args.cmd == "process":
        from hubos import process
        from hubos.store import Store

        st = Store()
        print(process.rescan(st, cfg, args.since) if args.rescan else process.ingest(st, cfg))
    elif args.cmd == "vault":
        from hubos import vault
        from hubos.store import Store

        print(vault.build(Store(), cfg, args.out or vault.OUT))
    elif args.cmd == "threads":
        from hubos import process, threads
        from hubos.store import Store

        st, chans, prof = Store(), process.channels(cfg), process.profile(cfg)
        if args.review or args.score or args.sample:
            all_cards = threads.cards(st, chans, prof)
            if args.review:
                path, n, done = threads.answered_review(all_cards, args.org)
                print(f"{n} hidden cards in {path} ({done} already labelled). Verdict: done, open, followup or someone-else.")
            if args.sample:
                if not args.org:
                    sys.exit("--sample needs --org")
                path, n, total = threads.mention_sample(st, all_cards, prof, args.org)
                print(f"{n} of {total} messages naming you in {path}. Fill in for_me: y (meant for me) or n.")
            r = {}
            if args.score:
                r = threads.answered_score(all_cards, args.org)
                if args.org:
                    r.update({"mentions_" + k: v for k, v in threads.mention_score(all_cards, args.org).items()})
        else:
            r = threads.report(st, chans, prof, cfg.get("me") or "me", args.samples)
        for k, v in r.items():
            print(f"{k:28} {v}")
    elif args.cmd == "live":
        from hubos import live

        print(live.ask_audit() if args.audit else json.dumps(live.report(args.days), indent=1))
    elif args.cmd == "config":
        import config_ui

        config_ui.serve(UI_PORT, not args.no_open)
    elif args.cmd == "discover":
        discover(cfg, args)
    else:
        keys = pick_orgs(cfg, args.orgs, args.all)
        if args.cmd in ("sync", "run"):
            sync(cfg, keys, args)
        if args.cmd in ("analyze", "run"):
            ok = [analyze(cfg, k, args) for k in keys]
            if not all(ok):
                sys.exit(1)


if __name__ == "__main__":
    main()
