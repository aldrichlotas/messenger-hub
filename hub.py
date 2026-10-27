"""Messenger Hub: sync your organisations' Messenger group chats and turn them into trackers,
roadmaps and briefs.

    py -3.10 hub.py app                                  the desktop app: today, deadlines, mentions, bot, settings
    py -3.10 hub.py process [--rescan]                   re-read synced chats with the rules (no tokens)
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
DISCOVER_ROUNDS = 40
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


def chat_dir(chat: dict):
    return os.path.join(CHATS, chat["slug"]) if chat.get("slug") else None


# --- browser commands ---------------------------------------------------------------------------


def discover(cfg: dict, args) -> None:
    from playwright.sync_api import sync_playwright

    import scrape

    with open(os.path.join(ROOT, "messenger", "discover.js"), encoding="utf-8") as f:
        script = f.read()
    with sync_playwright() as p:
        context = scrape.launch(p)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com/messages/", wait_until="domcontentloaded")
        deadline = time.time() + scrape.LOGIN_TIMEOUT
        told = False
        while time.time() < deadline:
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
        time.sleep(2)
        last, idle = 0, 0
        for _ in range(args.rounds):
            count = page.evaluate("window.__hsd.collect()")
            idle = idle + 1 if count == last else 0
            last = count
            if idle >= 4 or not page.evaluate("window.__hsd.scrollDown()"):
                break
            time.sleep(1.5)
        chats = page.evaluate("window.__hsd.list()")
        context.close()

    os.makedirs(DATA, exist_ok=True)
    with open(CHAT_LIST, "w", encoding="utf-8") as f:
        json.dump({"at": dt.datetime.now().isoformat(timespec="seconds"), "chats": chats}, f, ensure_ascii=False, indent=1)
    print(f"Found {len(chats)} chats (saved to {os.path.relpath(CHAT_LIST, ROOT)}).")

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


def sync(cfg: dict, keys: list, args) -> None:
    from playwright.sync_api import sync_playwright

    import scrape

    chats, seen = [], set()
    for k in keys:
        for c in active(cfg["orgs"][k]["chats"]):
            tid = thread_id(c["thread"])
            if tid not in seen:
                seen.add(tid)
                chats.append(c)
    me_chat = (cfg.get("profile") or {}).get("self_chat")
    if me_chat and thread_id(me_chat) not in seen:
        # Your notes-to-self thread: the capture inbox, synced with every run.
        chats.append({"thread": me_chat, "name": "Notes to self", "slug": (cfg.get("profile") or {}).get("self_slug")})
    if not chats:
        sys.exit("These orgs have no enabled chats: pick some with `hub.py config` or `chat add`.")
    with sync_playwright() as p:
        context = scrape.launch(p)
        for n, c in enumerate(chats, 1):
            print(f"\n=== [{n}/{len(chats)}] {c.get('name') or c['thread']}")
            try:
                res = scrape.scrape_thread(
                    context,
                    c["thread"],
                    out_dir=chat_dir(c),
                    me=cfg["me"],
                    since=args.since,
                    no_download=args.no_download,
                    max_minutes=args.max_minutes,
                    debug=args.debug,
                )
            except Exception as e:  # one broken chat shouldn't stop the rest
                print(f"! failed: {e}")
                continue
            slug = os.path.basename(res["out_dir"])
            if me_chat and thread_id(c["thread"]) == thread_id(me_chat):
                cfg.setdefault("profile", {})["self_slug"] = slug
            # The same chat may be listed in several orgs: record its slug everywhere.
            for o in cfg["orgs"].values():
                for oc in o["chats"]:
                    if thread_id(oc["thread"]) == thread_id(c["thread"]):
                        oc["slug"] = oc.get("slug") or slug
                        oc["name"] = oc.get("name") or res["title"]
            save_config(cfg)
        context.close()


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

    cf = sub.add_parser("config", help="edit orgs and their chats in a local web page")
    cf.add_argument("--no-open", action="store_true", help="don't open the page in your browser")

    c = sub.add_parser("chat", help="add, remove, enable or disable a chat")
    c.add_argument("action", choices=["add", "remove", "enable", "disable"])
    c.add_argument("org")
    c.add_argument("thread", help="https://www.facebook.com/messages/t/<id> (or e2ee/t/<id>) or the id")
    c.add_argument("--name")

    d = sub.add_parser("discover", help="list the chats in your Messenger sidebar (opens the browser)")
    d.add_argument("--match", help="only show chats whose name matches this regex")
    d.add_argument("--add", action="store_true", help="also add every chat matching an org's --match to it")
    d.add_argument("--rounds", type=int, default=DISCOVER_ROUNDS, help="how far to scroll the chat list")

    for name, hlp in (("sync", "scrape new messages"), ("analyze", "update reports"), ("run", "sync then analyze")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("orgs", nargs="*")
        s.add_argument("--all", action="store_true")
        if name in ("sync", "run"):
            s.add_argument("--since", help="how far back a chat's first sync goes (YYYY-MM-DD)")
            s.add_argument("--no-download", action="store_true")
            s.add_argument("--max-minutes", type=float, default=30, help="per chat")
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
        print((process.rescan if args.rescan else process.ingest)(st, cfg))
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
