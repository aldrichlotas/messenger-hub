"""Ingest synced chats into the store and derive entries and mentions with the rules. No tokens.

    py -3.10 hub.py process            ingest new messages
    py -3.10 hub.py process --rescan   re-run the rules over everything (after changing your profile)
"""

import datetime as dt
import json
import os
import uuid

from . import rules, threads
from .store import ROOT, Store, now_iso

CHATS = os.environ.get("HUB_CHATS") or os.path.join(ROOT, "data", "chats")  # HUB_CHATS: trial imports
# Unreviewed rule findings at or above this are treated as yours (shown, briefed, alerted).
SURE = 0.8
SUGGEST = 0.6
PLAN_CAP = 100  # rows of the Now list sent to the page; the counts stay true


def profile(cfg: dict) -> rules.Profile:
    prof = cfg.get("profile") or {}
    names = [n for n in (prof.get("names") or []) if n] or [cfg.get("me") or "You"]
    p = rules.Profile(names=names, not_me=prof.get("not_me") or [])
    for key, org in cfg.get("orgs", {}).items():
        p.titles[key] = [t for t in [org.get("my_title")] + (org.get("aliases") or []) if t]
        p.committees[key] = org.get("committees") or []
        p.above[key] = org.get("above") or []
    return p


def is_mine(sender, cfg: dict) -> bool:
    """You under any of your names: Messenger shows "Aldrich" in one chat and "Aldrich Lotas" in another."""
    names = {n.strip().lower() for n in [cfg.get("me") or ""] + ((cfg.get("profile") or {}).get("names") or []) if n and n.strip()}
    return (sender or "").strip().lower() in names


def channels(cfg: dict) -> dict:
    """slug -> {org, name, self} for every enabled synced chat."""
    out = {}
    for key, org in cfg.get("orgs", {}).items():
        for c in org.get("chats", []):
            if c.get("slug") and c.get("enabled", True) and c["slug"] not in out:
                out[c["slug"]] = {"org": key, "name": c.get("name") or c["slug"], "self": False}
    slug = (cfg.get("profile") or {}).get("self_slug")
    if slug:
        out[slug] = {"org": None, "name": "Notes to self", "self": True}
    return out


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# Messages from one sender less than this apart are read as one message ("@Aldrich" + "pubmat by Fri").
BURST = dt.timedelta(minutes=5)


def _context(st: Store, item_id: int, item: dict, ts: dt.datetime) -> tuple:
    """(burst, parent): the same sender's previous messages in this burst (newest first, others'
    messages in between are skipped) and the message this one replies to."""
    burst, t = [], ts
    for p in st.q(
        "SELECT * FROM item WHERE channel = ? AND id < ? AND ts >= ? ORDER BY id DESC LIMIT 20",
        (item["channel"], item_id, (ts - 3 * BURST).isoformat(timespec="minutes")),
    ):
        pt = _ts(p["ts"])
        if p["author"] != item["author"] or pt is None:
            continue
        if t - pt > BURST:
            break
        burst.append(p)
        t = pt
        if len(burst) == 3:
            break
    parent = None
    if item.get("reply_ext"):
        parent = st.one("SELECT * FROM item WHERE channel = ? AND ext_id = ?", (item["channel"], str(item["reply_ext"])))
        if parent and parent["is_me"]:
            parent = None  # "sige" under your own message isn't a new ask; it's in this text if it is
    return burst, parent


def _upgrade(st: Store, entry_id: int, found: rules.Finding, due, why: str) -> None:
    """A linked message made an earlier rule finding stronger: raise it, unless you reviewed it,
    or the message already has an entry of that kind (one you kept through a rescan)."""
    st.run(
        "UPDATE entry SET kind = ?, due = COALESCE(?, due), due_has_time = MAX(due_has_time, ?), confidence = ?,"
        " reasons = ?, updated = ? WHERE id = ? AND review = 'unreviewed' AND confidence < ?"
        " AND NOT EXISTS (SELECT 1 FROM entry o WHERE o.item_id = entry.item_id AND o.kind = ? AND o.id != entry.id)",
        (found.kind, due, 1 if found.has_time else 0, found.confidence, "; ".join(found.reasons + [why]), now_iso(),
         entry_id, found.confidence, found.kind),
    )


def _derive(st: Store, item_id: int, item: dict, ch: dict, prof: rules.Profile, me: str, now: dt.datetime, orgs=None,
            keep_since: dt.datetime = None) -> None:
    ts = _ts(item["ts"])
    if ts is None:
        return
    text = item["text"] or ""
    if ch["self"]:
        if item["is_me"]:
            add_capture_entry(st, item_id, item["ref"], text, ts, set(orgs or []))
        return
    is_me, org = bool(item["is_me"]), ch["org"]
    strength, reason = (0, "") if is_me else rules.mention(text, org, prof)
    own_strength = strength
    burst, parent = ([], None) if is_me else _context(st, item_id, item, ts)
    # Who it's for can come from a linked message: an earlier "@Aldrich" in the burst, or replying to you.
    for b in burst:
        s, r = rules.mention(b["text"], org, prof)
        if s > strength:
            strength, reason = s, f"{r}, in the message before"
    if not is_me and item.get("reply_to_me") and strength < 2:
        strength, reason = 2, "replies to you"
    found = rules.analyse(text, ts, item["author"], is_me, org, prof, strength)
    linked = None  # the earlier message the finding really belongs to
    if not found and not is_me and strength >= 2 and (burst or parent):
        # What's asked can come from a linked message: "pubmat for GA, due Fri" then "@Aldrich",
        # or "@Aldrich ikaw na" replying to the request.
        before = [b["text"] or "" for b in reversed(burst)] + ([parent["text"] or ""] if parent else [])
        found = rules.analyse("\n".join(before + [text]), ts, item["author"], False, org, prof, strength)
        if found:
            found.reasons.append("read with " + ("the message it replies to" if parent and not burst else "the messages around it"))
            linked = next((c for c in [parent] + burst if c and rules.analyse(c["text"], ts, c["author"], False, org, prof, strength)), None)
    if own_strength >= 2 or (strength >= 2 and found):  # "@everyone" alone isn't you: its deadline is an entry, not a card
        st.add_mention(item_id, org, strength, reason, item["ts"])
    if not found:
        return
    due = found.due.isoformat(timespec="minutes") if found.due else None
    if linked:
        old = st.one("SELECT id FROM entry WHERE item_id = ? AND origin = 'rule'", (linked["id"],))
        if old:
            return _upgrade(st, old["id"], found, due, "linked by " + (item["ref"] or "a later message"))
    # Old history: keep for search, but don't let it clutter the deadlines (unless a rescan asked to keep it).
    stale = (found.due or ts) < (keep_since or now - dt.timedelta(days=2))
    st.add_entry(
        kind=found.kind,
        title=rules.clean_title((linked or {}).get("text") or text),
        due=due,
        due_has_time=found.has_time,
        org=org,
        item_id=item_id,
        ref=item["ref"],
        status="past" if stale else "open",
        origin="rule",
        confidence=found.confidence,
        reasons="; ".join(found.reasons),
    )


def add_capture_entry(st: Store, item_id, ref, text: str, ts: dt.datetime, orgs=None) -> int:
    c = rules.capture(text, ts, orgs)
    org = next((t for t in c["tags"] if not orgs or t in orgs), None)
    return st.add_entry(
        kind=c["kind"],
        title=c["title"],
        due=c["due"].isoformat(timespec="minutes") if c["due"] else None,
        due_has_time=c["has_time"],
        org=org,
        item_id=item_id,
        ref=ref,
        status="done" if c["kind"] == "win" else "open",
        review="accepted",
        origin="capture",
        confidence=1.0,
    )


def capture(st: Store, text: str, source: str, cfg: dict) -> dict:
    """A note from the app's quick-add box or the Telegram bot."""
    now = dt.datetime.now()
    channel = "bot" if source == "telegram-bot" else "app"
    item_id, _ = st.add_item(
        source=source, channel=channel, ext_id=uuid.uuid4().hex, author=cfg.get("me"), is_me=True,
        ts=now.isoformat(timespec="minutes"), text=text, ref=f"{channel}:{now:%Y-%m-%d %H:%M}",
    )
    entry_id = add_capture_entry(st, item_id, f"{channel}:{now:%Y-%m-%d %H:%M}", text, now, set(cfg.get("orgs", {})))
    st.run("UPDATE entry SET origin = ? WHERE id = ?", ("bot" if channel == "bot" else "app", entry_id))
    return st.one("SELECT * FROM entry WHERE id = ?", (entry_id,))


def _media(m: dict):
    """The message's own saved attachments as JSON (a reply's thumbnail of the original is skipped)."""
    out = [{"type": a.get("type") or "file", "path": a["path"]} for a in m.get("attachments") or []
           if a.get("path") and not a.get("quoted")]
    return json.dumps(out) if out else None


def ingest(st: Store, cfg: dict, force: bool = False) -> dict:
    """Load new messages from every enabled synced chat. Cheap: skips files that haven't changed."""
    prof, me, now = profile(cfg), cfg.get("me") or "You", dt.datetime.now()
    stats = {"chats": 0, "new_items": 0}
    for slug, ch in channels(cfg).items():
        path = os.path.join(CHATS, slug, "messages.jsonl")
        if not os.path.exists(path):
            continue
        mtime = str(os.path.getmtime(path))
        if not force and st.get_meta(f"mtime:{slug}") == mtime:
            continue
        stats["chats"] += 1
        with st.batch(), open(path, encoding="utf-8") as f:  # one commit per chat: a first sync is thousands of rows
            for line in f:
                if not line.strip():
                    continue
                m = json.loads(line)
                if m.get("kind") != "message":
                    continue
                item = {
                    "source": "messenger", "channel": slug, "ext_id": m["id"], "org": ch["org"], "chat_name": ch["name"],
                    "author": m.get("sender"), "is_me": is_mine(m.get("sender"), cfg), "ts": m.get("ts"),
                    "text": m.get("text") or "", "ref": f"{slug}#{m['id']}", "reply_ext": m.get("reply_to"),
                    "reply_to_me": m.get("sender") != me
                    and (m.get("reply_target") == me or "replied to you" in (m.get("reply_header") or "")),
                    "media": _media(m), "links": json.dumps(m["links"]) if m.get("links") else None,
                }
                item_id, created = st.add_item(**item)
                if created:
                    stats["new_items"] += 1
                    _derive(st, item_id, item, ch, prof, me, now, cfg.get("orgs"))
        st.set_meta(f"mtime:{slug}", mtime)
    st.set_meta("last_process", now_iso())
    return stats


def rescan(st: Store, cfg: dict, since: str = None) -> dict:
    """Re-run the rules over every stored message, keeping what you already reviewed or read.
    With `since` (YYYY-MM-DD), findings from that date on stay open for review instead of "past"."""
    keep = dt.datetime.fromisoformat(since) if since else None
    ingest(st, cfg, force=True)  # also fills in reply links on messages stored before they were kept
    read = {r["item_id"] for r in st.q("SELECT item_id FROM mention WHERE read = 1")}
    prof, me, now, chans = profile(cfg), cfg.get("me") or "You", dt.datetime.now(), channels(cfg)
    n = 0
    with st.batch():  # one commit for the whole rebuild instead of one per row
        st.run("DELETE FROM entry WHERE origin = 'rule' AND review = 'unreviewed'")
        st.run("DELETE FROM mention")
        for it in st.q("SELECT * FROM item WHERE source = 'messenger' ORDER BY id"):
            ch = chans.get(it["channel"])
            if not ch:
                continue
            if bool(it["is_me"]) != is_mine(it["author"], cfg):  # e.g. "Aldrich Lotas" in one chat, "Aldrich" in another
                it["is_me"] = is_mine(it["author"], cfg)
                st.run("UPDATE item SET is_me = ? WHERE id = ?", (1 if it["is_me"] else 0, it["id"]))
            if ch["self"]:
                continue  # captures are already entries, and you may have edited them
            _derive(st, it["id"], it, ch, prof, me, now, cfg.get("orgs"), keep)
            n += 1
        st.run(f"UPDATE mention SET read = 1 WHERE item_id IN ({','.join(str(i) for i in read) or '0'})")
    return {"rescanned": n}


# --- views (also used by the bot) --------------------------------------------------------------

RELEVANT = f"(e.origin != 'rule' OR e.review = 'accepted' OR (e.review = 'unreviewed' AND e.confidence >= {SURE}))"


def entries(st: Store, where: str, args=()) -> list:
    return st.q(
        "SELECT e.*, i.author, i.chat_name, i.text AS source_text, i.ts AS source_ts FROM entry e"
        f" LEFT JOIN item i ON i.id = e.item_id WHERE {where}",
        args,
    )


def plan(cards: list, open_mine: list, now: dt.datetime) -> dict:
    """One list of what's on you: each commitment once, joined to its conversation, most urgent first.
    now: yours to act on. next: dated later. dealt: handled lately, so you don't have to remember."""
    clock, today = now.isoformat(timespec="minutes"), now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    week = (now.date() + dt.timedelta(days=7)).isoformat()
    dead = (now - dt.timedelta(days=2)).isoformat(timespec="minutes")
    recent = (now - dt.timedelta(days=7)).isoformat(timespec="minutes")
    by_item = {i: c for c in cards for i in c["ping_items"]}

    def tier(due, has_time, waiting, above):
        if not due:
            t = 9
        elif (due < clock if has_time else due[:10] < today):
            t = 0
        else:
            t = 1 if due[:10] == today else 3 if due[:10] == tomorrow else 5 if due[:10] < week else 6
        return min(t, 2 if above else 4) if waiting else t

    def word(t, c):
        if c and c["state"] == "followup":
            return c["why"]
        if c and c["state"] == "answered":
            return "You replied"
        return {0: "Overdue", 1: "Due today", 3: "Due tomorrow"}.get(t, "Waiting on you" if c else "To do")

    items, dealt, used = [], [], set()
    for e in open_mine:
        c = by_item.get(e["item_id"])
        if (e["kind"] == "capture" and not e["due"]) or (e["review"] == "unreviewed" and e["due"] and e["due"] < dead):
            continue
        if c and c["key"] in used:
            continue  # the same conversation: resolving it closes this entry too
        if c:
            used.add(c["key"])
        handled = c["handled"] if c and c["state"] == "answered" else None
        if handled and (not e["due"] or e["due"] < clock):
            continue  # listed with the handled conversations below
        waiting = bool(c) and c["state"] in ("needs", "followup")
        t = tier(e["due"], e["due_has_time"], waiting or not e["due"], bool(c and c["above"]))
        items.append({
            "key": f"e{e['id']}", "entry": e["id"], "card": c["key"] if c else None, "kind": e["kind"], "title": e["title"],
            "due": e["due"], "due_has_time": e["due_has_time"], "org": e["org"], "who": e["author"], "chat": e["chat_name"],
            "tier": t, "say": word(t, c), "unsure": e["review"] == "unreviewed", "source": None if c else e["source_text"], "origin": e["origin"],
        })
    for c in cards:
        if c["key"] in used or c["state"] not in ("needs", "followup"):
            continue
        t = tier(c["due"], True, True, c["above"])
        items.append({
            "key": "c" + c["key"], "entry": None, "card": c["key"], "kind": "mention", "title": c["summary"] or c["gist"], "due": c["due"],
            "due_has_time": 1, "org": c["org"], "who": c["asker"], "chat": c["chat_name"], "tier": t, "say": word(t, c),
            "unsure": False, "source": None, "origin": "rule",
        })
    items.sort(key=lambda x: (x["tier"], x["due"] or "9"))
    for c in cards:
        h = c["handled"]
        if c["state"] == "answered" and h and h["ts"] >= recent:
            dealt.append({"title": c["summary"] or c["gist"], "how": h["how"], "ts": h["ts"], "org": c["org"], "who": c["asker"], "card": c["key"],
                          "undo": h["how"] == "you marked it done"})
    dealt.sort(key=lambda x: x["ts"], reverse=True)
    mine, later = [i for i in items if i["tier"] <= 4], [i for i in items if i["tier"] > 4]
    return {"now": mine[:PLAN_CAP], "now_count": len(mine), "next": later[:PLAN_CAP], "next_count": len(later), "dealt": dealt[:8], "dealt_count": len(dealt)}


def brief(st: Store, cards: list, now: dt.datetime) -> dict:
    """What has happened in the last day, per org. Counts and titles only, so it costs no tokens."""
    since = (now - dt.timedelta(days=1)).isoformat(timespec="minutes")
    orgs = {}

    def org(key):
        return orgs.setdefault(key, {"org": key, "messages": 0, "pings": 0, "waiting": 0, "answered": 0})

    for r in st.q("SELECT org, COUNT(*) AS n FROM item WHERE source = 'messenger' AND ts >= ? GROUP BY org", (since,)):
        org(r["org"])["messages"] = r["n"]
    for c in cards:
        if c["last_ping"] >= since:
            row = org(c["org"])
            row["pings"] += 1
            row["waiting"] += c["state"] in ("needs", "followup")
            row["answered"] += c["state"] == "answered"
    found = st.q(
        "SELECT kind, title, org, due, due_has_time FROM entry WHERE created >= ? AND origin = 'rule' AND status = 'open'"
        " AND review != 'rejected' AND kind IN ('deadline', 'meeting') AND (due IS NULL OR due >= ?) ORDER BY due IS NULL, due LIMIT 6",
        (since, now.isoformat(timespec="minutes")),
    )
    return {"orgs": sorted(orgs.values(), key=lambda o: -o["messages"]), "found": found}


def dashboard(st: Store, now: dt.datetime = None, cfg: dict = None) -> dict:
    now = now or dt.datetime.now()
    conv_cards = threads.cards(st, channels(cfg), profile(cfg), now) if cfg else []
    today = now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    horizon = (now - dt.timedelta(days=14)).isoformat(timespec="minutes")
    open_mine = entries(
        st,
        f"e.status = 'open' AND e.review != 'rejected' AND e.kind != 'win' AND {RELEVANT} ORDER BY e.due IS NULL, e.due",
    )
    dated = [e for e in open_mine if e["due"] and e["due"] >= horizon]
    # Review is the unsure and still-possible; what's sure is already on your list, what's gone waits apart.
    unsure = f"e.status = 'open' AND e.review = 'unreviewed' AND e.origin = 'rule' AND e.confidence >= {SUGGEST}"
    gone = "(e.due < ? AND e.confidence < ?) OR e.due < ?"
    gone_args = (today, SURE, (now - dt.timedelta(days=2)).isoformat(timespec="minutes"))
    live = entries(
        st, f"{unsure} AND e.confidence < {SURE} AND NOT ({gone}) ORDER BY e.due IS NULL, e.due, e.confidence DESC LIMIT 40", gone_args
    )
    live_count = st.one(f"SELECT COUNT(*) AS n FROM entry e WHERE {unsure} AND e.confidence < {SURE} AND NOT ({gone})", gone_args)["n"]
    expired = entries(st, f"{unsure} AND ({gone}) ORDER BY e.due DESC LIMIT 40", gone_args)
    expired_count = st.one(f"SELECT COUNT(*) AS n FROM entry e WHERE {unsure} AND ({gone})", gone_args)["n"]
    return {
        "now": now.isoformat(timespec="minutes"),
        "today": [e for e in dated if e["due"][:10] == today],
        "tomorrow": [e for e in dated if e["due"][:10] == tomorrow],
        "overdue": [e for e in dated if e["due"] < now.isoformat(timespec="minutes") and e["due"][:10] != today],
        "deadlines": [e for e in dated if e["due"] >= now.isoformat(timespec="minutes")[:10]],
        "inbox": [e for e in open_mine if not e["due"]],
        "notes": [e for e in open_mine if e["kind"] == "capture"],
        "review": live,
        "review_expired": expired,
        "review_expired_count": expired_count,
        "review_count": live_count,
        "plan": plan(conv_cards, open_mine, now),
        "brief": brief(st, conv_cards, now),
        # One card per conversation that pings you (threads.py); handled ones never count as unread.
        "cards": conv_cards,
        "unread_mentions": sum(c["unread"] for c in conv_cards),
        "needs_you": sum(c["handled"] is None for c in conv_cards),
        "wins": entries(st, "e.kind = 'win' ORDER BY e.created DESC LIMIT 50"),
        "last_process": st.get_meta("last_process"),
    }
