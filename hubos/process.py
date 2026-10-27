"""Ingest synced chats into the store and derive entries and mentions with the rules. No tokens.

    py -3.10 hub.py process            ingest new messages
    py -3.10 hub.py process --rescan   re-run the rules over everything (after changing your profile)
"""

import datetime as dt
import json
import os
import uuid

from . import rules
from .store import ROOT, Store, now_iso

CHATS = os.path.join(ROOT, "data", "chats")
# Unreviewed rule findings at or above this are treated as yours (shown, briefed, alerted).
SURE = 0.8
SUGGEST = 0.6


def profile(cfg: dict) -> rules.Profile:
    prof = cfg.get("profile") or {}
    names = [n for n in (prof.get("names") or []) if n] or [cfg.get("me") or "You"]
    p = rules.Profile(names=names)
    for key, org in cfg.get("orgs", {}).items():
        p.titles[key] = [t for t in [org.get("my_title")] + (org.get("aliases") or []) if t]
        p.committees[key] = org.get("committees") or []
        p.above[key] = org.get("above") or []
    return p


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


def _derive(st: Store, item_id: int, item: dict, ch: dict, prof: rules.Profile, me: str, now: dt.datetime, orgs=None) -> None:
    ts = _ts(item["ts"])
    if ts is None:
        return
    text = item["text"] or ""
    if ch["self"]:
        if item["is_me"]:
            add_capture_entry(st, item_id, item["ref"], text, ts, set(orgs or []))
        return
    strength, reason = (0, "") if item["is_me"] else rules.mention(text, ch["org"], prof)
    found = rules.analyse(text, ts, item["author"], bool(item["is_me"]), ch["org"], prof, strength)
    if strength >= 2 or (strength == 1 and found):
        st.add_mention(item_id, ch["org"], strength, reason, item["ts"])
    if found:
        due = found.due.isoformat(timespec="minutes") if found.due else None
        # Old history: keep for search, but don't let it clutter the deadlines.
        stale = (found.due or ts) < now - dt.timedelta(days=2)
        st.add_entry(
            kind=found.kind,
            title=rules.clean_title(text),
            due=due,
            due_has_time=found.has_time,
            org=ch["org"],
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
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                m = json.loads(line)
                if m.get("kind") != "message":
                    continue
                item = {
                    "source": "messenger", "channel": slug, "ext_id": m["id"], "org": ch["org"], "chat_name": ch["name"],
                    "author": m.get("sender"), "is_me": m.get("sender") == me, "ts": m.get("ts"),
                    "text": m.get("text") or "", "ref": f"{slug}#{m['id']}",
                }
                item_id, created = st.add_item(**item)
                if created:
                    stats["new_items"] += 1
                    _derive(st, item_id, item, ch, prof, me, now, cfg.get("orgs"))
        st.set_meta(f"mtime:{slug}", mtime)
    st.set_meta("last_process", now_iso())
    return stats


def rescan(st: Store, cfg: dict) -> dict:
    """Re-run the rules over every stored message, keeping what you already reviewed or read."""
    read = {r["item_id"] for r in st.q("SELECT item_id FROM mention WHERE read = 1")}
    st.run("DELETE FROM entry WHERE origin = 'rule' AND review = 'unreviewed'")
    st.run("DELETE FROM mention")
    prof, me, now, chans = profile(cfg), cfg.get("me") or "You", dt.datetime.now(), channels(cfg)
    n = 0
    for it in st.q("SELECT * FROM item WHERE source = 'messenger' ORDER BY id"):
        ch = chans.get(it["channel"])
        if not ch:
            continue
        it["is_me"] = it["author"] == me
        if ch["self"]:
            continue  # captures are already entries, and you may have edited them
        _derive(st, it["id"], it, ch, prof, me, now, cfg.get("orgs"))
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


def dashboard(st: Store, now: dt.datetime = None) -> dict:
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    horizon = (now - dt.timedelta(days=14)).isoformat(timespec="minutes")
    open_mine = entries(
        st,
        f"e.status = 'open' AND e.review != 'rejected' AND e.kind != 'win' AND {RELEVANT} ORDER BY e.due IS NULL, e.due",
    )
    dated = [e for e in open_mine if e["due"] and e["due"] >= horizon]
    return {
        "now": now.isoformat(timespec="minutes"),
        "today": [e for e in dated if e["due"][:10] == today],
        "tomorrow": [e for e in dated if e["due"][:10] == tomorrow],
        "overdue": [e for e in dated if e["due"] < now.isoformat(timespec="minutes") and e["due"][:10] != today],
        "deadlines": [e for e in dated if e["due"] >= now.isoformat(timespec="minutes")[:10]],
        "inbox": [e for e in open_mine if not e["due"]],
        "review": entries(
            st,
            f"e.status = 'open' AND e.review = 'unreviewed' AND e.origin = 'rule' AND e.confidence >= {SUGGEST}"
            " ORDER BY e.confidence DESC, e.due IS NULL, e.due LIMIT 200",
        ),
        "review_count": st.one(
            f"SELECT COUNT(*) AS n FROM entry WHERE status = 'open' AND review = 'unreviewed' AND origin = 'rule' AND confidence >= {SUGGEST}"
        )["n"],
        "mentions": st.q(
            "SELECT m.id, m.strength, m.reason, m.read, m.org, i.author, i.chat_name, i.text, i.ts, i.ref FROM mention m"
            " JOIN item i ON i.id = m.item_id WHERE i.ts >= ? ORDER BY m.read, i.ts DESC LIMIT 300",
            ((now - dt.timedelta(days=30)).isoformat(timespec="minutes"),),
        ),
        "unread_mentions": st.one("SELECT COUNT(*) AS n FROM mention WHERE read = 0")["n"],
        "wins": entries(st, "e.kind = 'win' ORDER BY e.created DESC LIMIT 50"),
        "last_process": st.get_meta("last_process"),
    }
