"""Conversations and mention cards. No tokens (the one-line summaries are optional, see summarise()).

A busy group chat interleaves several conversations. They're split with the usual chat
disentanglement signals: a silence of GAP starts a new conversation, and a reply joins the
conversation of the message it answers (up to REPLY_REACH back). So a ping lands with the
messages that explain it.

Each conversation that pings you becomes one card: what kind of ask it is, whether you already
handled it, and the few messages that explain it.

    py -3.10 hub.py threads --report     check the split and the handled flags; writes 10 sample cards
"""

import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import urllib.parse

from . import rules
from .store import ROOT, Store, now_iso

GAP = dt.timedelta(minutes=30)  # silence that ends a conversation
REPLY_REACH = dt.timedelta(hours=24)  # a reply rejoins its conversation up to this long after it went quiet
BURST = dt.timedelta(minutes=5)  # one sender's messages this close together are one message
REPLIED = dt.timedelta(minutes=60)  # you posting this soon after a ping counts as answering it
WINDOW_DAYS = 30
SETTLES = dt.timedelta(days=7)  # your reply to a newer card settles an older one from the same person this far back
SETTLE_WORDS = 3
TOPIC_STOP = set("hello good morning afternoon evening thank thanks please kindly would like have this that with from your "
                 "will need help here there what when just hehe sorry also them they about".split())
REPORTS = os.path.join(ROOT, "data", "reports")

QUESTION = re.compile(
    r"\?|\b(ano|anong|sino|saan|nasaan|kailan|bakit|paano|pano|pwede|puwede|can you|could you|will you|would you|are you|"
    r"r u|do you|did you|have you|what|when|where|who|how|why|which|tapos na|ready na)\b",
    re.I,
)
ASK = re.compile(
    r"\b(pls|plz|please|paki\w*|pa-\w+|need you|help|check|review|send|upload|post|fix|join|gawa|gawin|ikaw na|mo na|turn on|"
    r"kindly|bump|bumping|note|update|pahelp|waiting (?:lang )?on|step up|facilitate)\b",  # how an org's EB asks
    re.I,
)
BANTER = re.compile(
    r"^[\W\d_]*(?:(?:ha){2,}h?|a?haha\w*|lol+|lmao+|xd+|hehe+|hihi+|sticker\w*|gg|nice|oks?|okay|sige|noted|ty|thanks?|"
    r"thank you|salamat)?[\W\d_]*$",
    re.I,
)


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def split(items: list, use_replies: bool = True) -> list:
    """Conversations in one channel's items (sorted by time): [{key, channel, msgs, last}]."""
    convs, by_ext, cur = [], {}, None
    for it in items:
        ts = _ts(it["ts"])
        if ts is None:
            continue
        it = dict(it, _t=ts)
        target = None
        parent = by_ext.get(str(it["reply_ext"])) if use_replies and it.get("reply_ext") else None
        if parent is not None and ts - parent["last"] <= REPLY_REACH:
            target = parent
        elif cur is not None and ts - cur["last"] <= GAP:
            target = cur
        if target is None:
            target = {"key": f"{it['channel']}#{it['ext_id']}", "channel": it["channel"], "msgs": [], "last": ts}
            convs.append(target)
        target["msgs"].append(it)
        target["last"] = max(target["last"], ts)
        by_ext[str(it["ext_id"])] = target
        cur = target
    return convs


def strip_names(text: str, prof: rules.Profile) -> str:
    rx, other = rules._words_rx(prof.names), rules._words_rx(prof.not_me)
    text = text or ""
    # Keep a namesake whole: "@Aldrich Ty" must not read as "Ty" with your name taken out.
    parts = other.split(text) if other else [text]
    text = "".join(p if n % 2 or not rx else rx.sub("", p) for n, p in enumerate(parts))
    return re.sub(r"@\s*", "", text).strip()


# Where a shared link goes, said plainly ("Canva design"): often that's what the ask is about.
SITES = [
    ("canva.com", "Canva design"), ("docs.google.com/document", "Google Doc"), ("docs.google.com/spreadsheets", "Google Sheet"),
    ("docs.google.com/presentation", "Google Slides"), ("docs.google.com/forms", "Google Form"), ("forms.gle", "Google Form"),
    ("drive.google.com", "Drive file"), ("figma.com", "Figma"), ("github.com", "GitHub"), ("zoom.us", "Zoom"),
    ("meet.google.com", "Google Meet"), ("tiktok.com", "TikTok"), ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("instagram.com", "Instagram"), ("facebook.com", "Facebook"), ("fb.watch", "Facebook"), ("roblox.com", "Roblox"),
]
DESIGN_SITES = ("Canva design", "Figma", "Drive file", "Google Slides", "Google Doc")


def site(url: str) -> str:
    u = url.lower()
    for k, name in SITES:
        if k in u:
            return name
    return urllib.parse.urlsplit(url).netloc.removeprefix("www.") or "link"


def media_of(m: dict) -> list:
    """The message's saved attachments as URLs the app serves (/media/<chat>/attachments/...)."""
    raw = m.get("media")
    if not raw or raw == "[]":  # most messages: cards() calls this tens of thousands of times
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [{"type": a["type"], "url": f"/media/{m['channel']}/" + urllib.parse.quote(a["path"].replace(os.sep, "/"))} for a in items]


def links_of(m: dict) -> list:
    raw = m.get("links")
    if not raw or raw == "[]":
        return []
    try:
        urls = json.loads(raw)
    except ValueError:
        return []
    return [{"url": u, "site": site(u)} for u in dict.fromkeys(urls) if u.startswith("http")]


def _line(m: dict, role: str = "") -> dict:
    media, links = media_of(m), links_of(m)
    t = (m["text"] or "").strip()
    for ln in links:  # a bare link is shown as its chip
        if t == ln["url"] or t.startswith(ln["url"] + "\n"):
            t = t[len(ln["url"]) :].strip()
    return {"author": m["author"], "is_me": bool(m["is_me"]), "text": t if len(t) <= 400 else t[:399] + "…",
            "ts": m["ts"], "ref": m["ref"], "role": role, "media": media, "links": links,
            "unsaved": not t and not media and not links}  # an attachment the export has no file for


def _dedupe(lines: list) -> list:
    """The export sometimes has a message twice in a row (a photo and its caption): show it once."""
    out = []
    for ln in lines:
        prev = out[-1] if out else None
        # The scraper sometimes repeats a few messages (A B A B): same sender, text and minute = one message.
        twin = "gap" not in ln and next((p for p in out[-4:] if "gap" not in p and (p["author"], p["text"], p["ts"][:16])
                                         == (ln["author"], ln["text"], ln["ts"][:16])), None)
        if twin and twin.get("media") == ln.get("media"):
            twin["role"] = twin["role"] or ln["role"]
            continue
        if prev and "gap" not in ln and "gap" not in prev and not ln["text"] and prev["author"] == ln["author"] \
                and prev["ts"][:16] == ln["ts"][:16]:
            # A photo sent with the text above: one message with both.
            prev["media"] = prev["media"] + [x for x in ln["media"] if x not in prev["media"]]
            prev["links"] = prev["links"] + [x for x in ln["links"] if x not in prev["links"]]
            prev["role"] = prev["role"] or ln["role"]
            continue
        out.append(dict(ln))
    return out


def _hash(msgs: list) -> str:
    return hashlib.sha1("\n".join(f"{m['ext_id']}|{m['text']}" for m in msgs).encode("utf-8")).hexdigest()[:16]


def _card(conv: dict, ping_ids: set, mentions: dict, entries: dict, prof: rules.Profile, org, state: dict) -> dict:
    msgs = conv["msgs"]
    idx = {m["id"]: i for i, m in enumerate(msgs)}
    pings, seen = [], set()
    for m in msgs:
        if m["id"] in ping_ids and (m["author"], m["text"], m["ts"][:16]) not in seen:  # skip the export's repeats
            seen.add((m["author"], m["text"], m["ts"][:16]))
            pings.append(m)

    def my_reply_after(p):
        """Your answer to a ping: a reply to it, or anything you said within REPLIED of it."""
        return next((m for m in msgs[idx[p["id"]] + 1 :] if m["is_me"]
                     and (str(m.get("reply_ext")) == str(p["ext_id"]) or m["_t"] - p["_t"] <= REPLIED)), None)

    open_pings = [p for p in pings if not my_reply_after(p)]
    # The first open ping that asks something: "noted, thanks Aldrich" then "@Aldrich please check tg" is about the second.
    def with_burst(p):  # the ping and what its author added right after ("hi @Aldrich" + "please check tg")
        out, t = [p], p["_t"]
        for m in msgs[idx[p["id"]] + 1 :]:
            if m["author"] != p["author"] or m["_t"] - t > BURST:
                break
            out.append(m)
            t = m["_t"]
        return " ".join(strip_names(m["text"] or "", prof) for m in out)

    asking = [p for p in open_pings if (lambda t: QUESTION.search(t) or ASK.search(t))(with_burst(p))]
    focus = (asking or open_pings or pings[-1:])[0]
    i = idx[focus["id"]]
    # What the asker said around the ping: their burst, and the message it replies to.
    burst, t = [focus], focus["_t"]
    for m in reversed(msgs[:i]):
        if m["author"] == focus["author"] and t - m["_t"] <= BURST:
            burst.insert(0, m)
            t = m["_t"]
    t = focus["_t"]
    for m in msgs[i + 1 :]:
        if m["author"] == focus["author"] and m["_t"] - t <= BURST:
            burst.append(m)
            t = m["_t"]
        elif m["is_me"]:
            break
    parent = next((m for m in msgs if focus.get("reply_ext") and str(m["ext_id"]) == str(focus["reply_ext"])), None)
    ask_text = " ".join(strip_names(m["text"], prof) for m in burst).strip()

    ent = next((entries[m["id"]] for m in burst + ([parent] if parent else []) if m["id"] in entries), None)
    if ent:
        label, due = ("meeting" if ent["kind"] == "meeting" else "request"), ent["due"]
    else:
        due = None
        both = ask_text + " " + strip_names((parent or {}).get("text") or "", prof) if parent and not parent["is_me"] else ask_text
        if QUESTION.search(ask_text):
            label = "question"
        elif ASK.search(ask_text) or rules.REQUEST.search(both):
            label = "request"
        elif not ask_text:
            label = "ping"  # just your name: they want your attention
        elif BANTER.match(ask_text):
            label = "banter"
        else:
            label = "fyi"

    # What they shared with the ask: their own photos/files and links, or those of the message replied to.
    shared = [m for m in burst + ([parent] if parent and not parent["is_me"] else [])]
    att = [x for m in shared for x in media_of(m)]
    links = [x for m in shared for x in links_of(m)]
    if label in ("question", "request", "ping") and (att or any(x["site"] in DESIGN_SITES for x in links)):
        label = "review"  # "@Aldrich" + a pubmat: look at this and say yes or no

    handled = None
    if not open_pings:
        r = my_reply_after(pings[-1])
        handled = {"how": "you replied", "ts": r["ts"]}
    elif label == "question" and mentions.get(focus["id"], {}).get("strength", 3) < 3 and not focus["reply_to_me"]:
        other = next((m for m in msgs[i + 1 :] if not m["is_me"] and m["author"] != focus["author"]
                      and (str(m.get("reply_ext")) == str(focus["ext_id"]))), None)
        if other:
            handled = {"how": f"answered by {other['author']}", "ts": other["ts"]}
    elif label in ("request", "review") and mentions.get(focus["id"], {}).get("strength", 3) < 3:
        # Asked of the committee, not of you by name: a teammate saying they'll do it settles it.
        other = next((m for m in msgs[i + 1 :] if not m["is_me"] and m["author"] != focus["author"]
                      and rules.TOOK_IT.search(m["text"] or "")), None)
        if other:
            handled = {"how": f"taken by {other['author']}", "ts": other["ts"]}
    # Replying isn't always being done: read what came after your reply.
    # Once you mark a follow-up done, only what's said after that can bring it back.
    why, done = None, (state.get(conv["key"]) or {}).get("done_upto") or ""
    if handled and handled["how"] == "you replied":
        since = msgs[idx[my_reply_after(pings[-1])["id"]] :]
        last_mine = [m for m in since if m["is_me"]][-1]
        after = [m for m in since if m["ts"] > done]
        asked = lambda m: (lambda t: QUESTION.search(t) or ASK.search(t) or rules.QUESTION_START.match(t))(strip_names(m["text"] or "", prof))
        if any(m["author"] == focus["author"] and m["_t"] > last_mine["_t"] and asked(m) for m in after):
            handled, why = None, "asked again after your reply"
        elif any(m["is_me"] and rules.LATER.search(m["text"] or "") for m in after) and not (
                rules.DONE_NOW.search(last_mine["text"] or "") and not rules.LATER.search(last_mine["text"] or "")):
            why = "you said you'd get to it"  # unless your last word is that it's finished
        elif any(not m["is_me"] and rules.WAITING.search(m["text"] or "") for m in after):
            why = "they're waiting on you"
        elif not done and label in ("request", "review") and all(BANTER.match(m["text"] or "") for m in since if m["is_me"]):
            why = "you only said thanks"  # "kindly change the password" → "thank you!" isn't done
        elif done:
            handled = {"how": "you marked it done", "ts": done}
    if not handled and not why and done and pings[-1]["ts"] <= done:  # done without a reply: you dealt with it elsewhere
        handled = {"how": "you marked it done", "ts": done}
    if not handled and not why and label in ("banter", "fyi"):
        handled = {"how": "nothing asked", "ts": focus["ts"]}
    # Nothing was asked (FYI, banter) is its own group even when you did reply.
    state_ = ("needs" if handled is None else "followup" if why
              else "fyi" if label in ("banter", "fyi") or handled["how"] == "nothing asked" else "answered")
    # The one line you read at a glance: the ask in their words, without your name.
    gist = re.sub(r"\s+", " ", ask_text).strip(" ,.:-") or (
        "re: " + strip_names(parent["text"], prof) if parent and (parent["text"] or "").strip() else "")
    if not gist:
        gist = (f"sent {len(att)} photo{'s' if len(att) != 1 else ''}" if att else "said your name")
    gist = gist if len(gist) <= 140 else gist[:139] + "…"
    # The same ask as separate messages, so the app can show where one ends and the next begins.
    asks = [t if len(t) <= 140 else t[:139] + "…" for t in
            (re.sub(r"\s+", " ", strip_names(m["text"], prof)).strip(" ,.:-") for m in burst) if re.search(r"\w", t)]

    # The excerpt: two messages of lead-up, the ask, what came right after, and your reply if later.
    shown = set(range(max(0, idx[burst[0]["id"]] - 2), min(len(msgs), idx[burst[-1]["id"]] + 3)))
    if parent and parent["id"] in idx:
        shown.add(idx[parent["id"]])
    mine = my_reply_after(focus)
    if mine:
        shown.add(idx[mine["id"]])
    roles = {m["id"]: "ask" for m in burst} | {p["id"]: "ping" for p in pings}
    if mine:
        roles[mine["id"]] = "reply"
    excerpt, last = [], None
    for k in sorted(shown)[:9]:
        if last is not None and k > last + 1:
            excerpt.append({"gap": k - last - 1})
        excerpt.append(_line(msgs[k], roles.get(msgs[k]["id"], "")))
        last = k
    excerpt = _dedupe(excerpt)
    st = state.get(conv["key"]) or {}
    last_ping = max(p["ts"] for p in pings)
    h = _hash(msgs)
    return {
        "key": conv["key"],
        "channel": conv["channel"],
        "org": org,
        "chat_name": focus["chat_name"],
        "label": label,
        "state": state_,
        "why": why,
        "gist": gist,
        "asks": asks,
        "due": due,
        "handled": handled,
        "media": att[:3],
        "media_count": len(att),
        "links": links[:3],
        "asker": focus["author"],
        "above": rules.is_above(focus["author"], org, prof),
        "pings": len(pings),
        "ping_items": sorted(ping_ids),  # every row, repeats included, so marking read clears them all
        "open_items": [p["id"] for p in open_pings],
        "last_ping": last_ping,
        "start": msgs[0]["ts"],
        "end": msgs[-1]["ts"],
        "size": len(msgs),
        "unread": handled is None and last_ping > (st.get("read_upto") or ""),
        "excerpt": excerpt,
        "full": _dedupe([_line(m, roles.get(m["id"], "")) for m in msgs[-60:]]),
        "summary": st.get("summary") if st.get("summary_hash") == h else None,
        "hash": h,
    }


def conversations(st: Store, chans: dict, since: str, use_replies: bool = True) -> list:
    rows = st.q("SELECT * FROM item WHERE source = 'messenger' AND ts >= ? ORDER BY channel, ts, id", (since,))
    out, by_chan = [], {}
    for r in rows:
        by_chan.setdefault(r["channel"], []).append(r)
    for chan, items in by_chan.items():
        ch = chans.get(chan)
        if ch and not ch["self"]:
            out += [dict(c, org=ch["org"]) for c in split(items, use_replies)]
    return out


def _topic(card: dict, people: set) -> set:
    said = " ".join(m["text"] or "" for m in card["full"] if m.get("author") == card["asker"])
    return {w for w in re.findall(r"\w{4,}", said.lower()) if w not in TOPIC_STOP and w not in people}


def _settle_by_later(cards_: list, people: set, now: dt.datetime) -> None:
    """An open card is settled if you replied to a newer card from the same person in the same chat,
    within SETTLES, sharing SETTLE_WORDS words that aren't names: "I'll need help with the JO recruitment
    application", then "here's the JO recruitment application brief". A deadline still ahead stays open."""
    replied, topics = {}, {}
    for n in cards_:
        if (n["handled"] or {}).get("how") == "you replied":
            replied.setdefault((n["channel"], n["asker"]), []).append(n)

    def topic(card):
        return topics.setdefault(card["key"], _topic(card, people)) if card["key"] not in topics else topics[card["key"]]

    today = now.isoformat(timespec="minutes")
    for c in cards_:
        newer = replied.get((c["channel"], c["asker"]))
        if not newer or c["state"] != "needs" or (c["due"] and c["due"] >= today):
            continue
        limit = _plus(c["last_ping"], SETTLES)
        for n in newer:
            if c["last_ping"] < n["last_ping"] <= limit and len(topic(c) & topic(n)) >= SETTLE_WORDS:
                c["handled"], c["state"] = {"how": "you replied later", "ts": n["handled"]["ts"]}, "answered"
                break


def _plus(ts: str, delta: dt.timedelta) -> str:
    return (dt.datetime.fromisoformat(ts) + delta).isoformat(timespec="minutes")


def cards(st: Store, chans: dict, prof: rules.Profile, now: dt.datetime = None, days: int = WINDOW_DAYS) -> list:
    """One card per conversation that pings you, most urgent first."""
    now = now or dt.datetime.now()
    since = (now - dt.timedelta(days=days)).isoformat(timespec="minutes")
    mentions = {r["item_id"]: r for r in st.q("SELECT * FROM mention")}
    entries = {
        r["item_id"]: r
        for r in st.q(
            "SELECT item_id, kind, due FROM entry WHERE origin = 'rule' AND review != 'rejected' AND kind IN ('task', 'deadline', 'meeting')"
            " AND item_id IS NOT NULL ORDER BY confidence DESC"
        )
    }
    state = {r["key"]: r for r in st.q("SELECT * FROM conv_state")}
    out = []
    for conv in conversations(st, chans, (now - dt.timedelta(days=days + 1)).isoformat(timespec="minutes")):
        ping_ids = {m["id"] for m in conv["msgs"] if not m["is_me"] and (m["id"] in mentions or m["reply_to_me"])}
        if not ping_ids or max(m["ts"] for m in conv["msgs"] if m["id"] in ping_ids) < since:
            continue
        out.append(_card(conv, ping_ids, mentions, entries, prof, conv["org"], state))
    people = {w for r in st.q("SELECT DISTINCT author FROM item WHERE author IS NOT NULL") for w in re.findall(r"\w{4,}", r["author"].lower())}
    _settle_by_later(out, people | {w for n in prof.names for w in n.lower().split()}, now)
    out.sort(key=lambda c: c["last_ping"], reverse=True)
    out.sort(key=lambda c: (c["handled"] is not None, not c["above"], c["due"] is None))
    return out


def mark_read(st: Store, key: str, upto: str, item_ids=()) -> None:
    """Pings in this conversation up to `upto` are read (new pings later make it unread again)."""
    st.run(
        "INSERT INTO conv_state (key, read_upto) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET read_upto = excluded.read_upto",
        (key, upto),
    )
    if item_ids:  # keep the per-message flags in step (rescan preserves them)
        st.run(f"UPDATE mention SET read = ? WHERE item_id IN ({','.join(str(int(i)) for i in item_ids)})", (1 if upto else 0,))


def mark_done(st: Store, key: str, upto) -> None:
    """You dealt with it: it moves to Answered until someone pings you again (upto None: undo)."""
    st.run(
        "INSERT INTO conv_state (key, done_upto) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET done_upto = excluded.done_upto",
        (key, upto),
    )


def log_event(st: Store, kind: str, ref: str) -> None:
    st.run("INSERT INTO event (kind, ref, at) VALUES (?, ?, ?)", (kind, ref, now_iso()))


# --- optional summaries (tokens: one Haiku call per unhandled conversation, cached) ---------------


def summarise(st: Store, card: dict, me: str) -> str:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("the claude CLI isn't on PATH")
    transcript = "\n".join(f"[{m['ts'][5:16]}] {m['author']}: {m['text']}" for m in card["full"][-40:])[-6000:]
    prompt = (
        f"Below is one conversation from a group chat. {me} was mentioned in it or replied to.\n"
        f"In one line of at most 25 words, say what is being asked of {me} (or that nothing is), "
        f"and whether {me} already answered. Plain text, no preamble.\n\n{transcript}"
    )
    res = subprocess.run(
        [claude, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--no-session-persistence"],
        input=prompt, text=True, encoding="utf-8", capture_output=True, timeout=180,
        cwd=tempfile.gettempdir(),  # away from this project, so its CLAUDE.md isn't read in (fewer tokens)
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError((res.stderr or res.stdout or "claude failed").strip()[:200])
    line = res.stdout.strip().splitlines()[0].strip()[:300]
    st.run(
        "INSERT INTO conv_state (key, summary, summary_hash) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET summary = excluded.summary, summary_hash = excluded.summary_hash",
        (card["key"], line, card["hash"]),
    )
    return line


def summarise_pending(st: Store, all_cards: list, me: str, limit: int = 5) -> int:
    """Background job when summaries are on: the newest unhandled cards without a fresh summary."""
    n = 0
    for c in all_cards:
        if n >= limit:
            break
        if c["handled"] is None and not c["summary"]:
            summarise(st, c, me)
            n += 1
    return n


# --- checks -------------------------------------------------------------------------------------


def media_check(st: Store, chans: dict) -> dict:
    """Every attachment the export saved: is the file there, non-empty, and stored where the app serves it from?"""
    from . import process

    out = {"in_export": 0, "missing": [], "empty": [], "not_in_store": 0}
    stored = set()
    for r in st.q("SELECT channel, media FROM item WHERE media IS NOT NULL"):
        for a in json.loads(r["media"]):
            stored.add((r["channel"], a["path"]))
    for slug in chans:
        path = os.path.join(process.CHATS, slug, "messages.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                for a in m.get("attachments") or []:
                    if not a.get("path") or a.get("quoted"):
                        continue
                    out["in_export"] += 1
                    full = os.path.join(process.CHATS, slug, a["path"])
                    if not os.path.isfile(full):
                        out["missing"].append(f"{slug}/{a['path']}")
                    elif not os.path.getsize(full):
                        out["empty"].append(f"{slug}/{a['path']}")
                    out["not_in_store"] += (slug, a["path"]) not in stored
    out["broken"] = len(out["missing"]) + len(out["empty"])
    out["missing"], out["empty"] = out["missing"][:10], out["empty"][:10]
    return out


def report(st: Store, chans: dict, prof: rules.Profile, me: str, samples: int = 10, now: dt.datetime = None) -> dict:
    """Numbers that don't depend on anyone's opinion, plus sample cards for a cold reader."""
    now = now or dt.datetime.now()
    convs = conversations(st, chans, "0000")
    where = {}
    for c in convs:
        for m in c["msgs"]:
            where[(c["channel"], str(m["ext_id"]))] = c["key"]
    # 1. Reply links were made by the senders themselves: a good split keeps each reply with its message.
    linked = same = 0
    for c in convs:
        for m in c["msgs"]:
            if m.get("reply_ext") and (c["channel"], str(m["reply_ext"])) in where:
                linked += 1
                same += where[(c["channel"], str(m["reply_ext"]))] == c["key"]
    # The same, splitting on silence alone: how well conversations hold together where nobody used Reply.
    gap_where = {(c["channel"], str(m["ext_id"])): c["key"] for c in conversations(st, chans, "0000", False) for m in c["msgs"]}
    gap_same = sum(gap_where.get((c["channel"], str(m["reply_ext"]))) == gap_where[(c["channel"], str(m["ext_id"]))]
                   for c in convs for m in c["msgs"] if m.get("reply_ext") and (c["channel"], str(m["reply_ext"])) in where)
    # 2. Handled vs the log: pings you explicitly replied to (Messenger's reply button) must show handled.
    all_cards = cards(st, chans, prof, now, days=3650)
    by_item = {i: c for c in all_cards for i in c["ping_items"]}
    explicit = [m for c in convs for m in c["msgs"] if m["is_me"] and m.get("reply_ext")]
    targets = {(m["channel"], str(m["reply_ext"])) for m in explicit}
    checked = agree = 0
    for c in convs:
        for m in c["msgs"]:
            if m["id"] in by_item and (c["channel"], str(m["ext_id"])) in targets:
                checked += 1
                card = by_item[m["id"]]
                agree += m["id"] not in card["open_items"]
    labels = {}
    for c in all_cards:
        labels[c["label"]] = labels.get(c["label"], 0) + 1
    week = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")
    out = {
        "conversations": len(convs),
        "cards": len(all_cards),
        "unhandled": sum(c["handled"] is None for c in all_cards),
        "labels": labels,
        "reply_cohesion": f"{same}/{linked}" + (f" = {same / linked:.0%}" if linked else ""),
        "reply_cohesion_gap_only": f"{gap_same}/{linked}" + (f" = {gap_same / linked:.0%}" if linked else ""),
        "handled_vs_explicit_replies": f"{agree}/{checked}" + (f" = {agree / checked:.0%}" if checked else ""),
        "opened_in_messenger_7d": st.one("SELECT COUNT(*) AS n FROM event WHERE kind = 'open-source' AND at >= ?", (week,))["n"],
        "mentions_opened_7d": st.one("SELECT COUNT(*) AS n FROM event WHERE kind = 'mentions-open' AND at >= ?", (week,))["n"],
        # Opening what Home hides: often means you don't trust "answered" to be done.
        "home_answered_opened_7d": st.one("SELECT COUNT(*) AS n FROM event WHERE kind = 'home-answered' AND at >= ?", (week,))["n"],
    }
    clears = sorted(int(r["ref"]) for r in st.q("SELECT ref FROM event WHERE kind = 'mentions-clear' AND at >= ?", (week,))
                    if (r["ref"] or "").isdigit())
    # Your own behaviour as the answer key: messages you answered with Messenger's Reply this week that never got a
    # card are misses no label file would show. And how many new "Needs you" a day there are to clear.
    by_ext = {(c["channel"], str(m["ext_id"])): m for c in convs for m in c["msgs"]}
    replied = {(m["channel"], str(m["reply_ext"])) for m in explicit if m["ts"] >= week[:16]}
    targets_7d = [by_ext[t] for t in replied if t in by_ext and not by_ext[t]["is_me"]]
    out["your_replies_7d"] = len(targets_7d)
    out["your_replies_without_card_7d"] = sorted(m["id"] for m in targets_7d if m["id"] not in by_item)
    out["needs_per_day_7d"] = round(sum(c["state"] == "needs" and c["last_ping"] >= week[:16] for c in all_cards) / 7, 1)
    out["cleared_7d"] = len(clears)
    out["median_seconds_to_clear"] = clears[len(clears) // 2] if clears else None
    out["attachments"] = media_check(st, chans)
    out["samples_file"] = write_samples(all_cards, samples, me)
    return out


# --- is "answered" right? You label what Home hides; the rules are scored against your labels ----------

REVIEW_COLS = ["key", "state", "how", "chat", "asker", "above", "label", "gist", "your_reply", "after_your_reply", "verdict", "note"]
VERDICTS = {"d": "done", "o": "open", "f": "followup", "s": "someone-else"}  # first letter is enough
OK_HIDDEN = {"done", "someone-else"}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _review_path(org: str = None) -> str:
    return os.path.join(REPORTS, f"answered-review-{org}.csv" if org else "answered-review.csv")


def _read_review(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["key"]: r for r in csv.DictReader(f)}


def answered_review(all_cards: list, org: str = None) -> tuple:
    """Write every card Home hides (answered or FYI) to data/reports/answered-review.csv for you to label.

    Verdicts already in the file are kept, so you can re-export after a sync without losing them."""
    path = _review_path(org)
    old = _read_review(path)
    rows = []
    for c in all_cards:
        if c["state"] == "needs" or (org and c["org"] != org):
            continue
        lines = [ln for ln in c["full"] if "gap" not in ln]
        at = (c["handled"] or {}).get("ts") or ""
        mine = next((i for i, ln in enumerate(lines) if ln["is_me"] and ln["ts"] >= at), None) if at else None
        after = lines[mine + 1 :][:6] if mine is not None else []
        prev = old.get(c["key"], {})
        rows.append({
            "key": c["key"], "state": c["state"], "how": (c["handled"] or {}).get("how", ""), "chat": c["chat_name"],
            "asker": c["asker"], "above": "yes" if c["above"] else "", "label": c["label"], "gist": c["gist"],
            "your_reply": _flat(lines[mine]["text"]) if mine is not None else "",
            "after_your_reply": " | ".join(f"{'You' if ln['is_me'] else ln['author']}: {_flat(ln['text'])}" for ln in after),
            "verdict": prev.get("verdict", ""), "note": prev.get("note", ""),
        })
    os.makedirs(REPORTS, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # -sig: Excel reads the Filipino and emoji right
        w = csv.DictWriter(f, REVIEW_COLS)
        w.writeheader()
        w.writerows(rows)
    return path, len(rows), sum(bool(r["verdict"]) for r in rows)


def answered_score(all_cards: list, org: str = None) -> dict:
    """Precision of hiding, from your verdicts: then (as labelled) and now (with today's rules)."""
    labelled = {k: VERDICTS.get(r["verdict"].strip().lower()[:1], "") for k, r in _read_review(_review_path(org)).items()}
    labelled = {k: v for k, v in labelled.items() if v}
    now = {c["key"]: c["state"] for c in all_cards}
    misses = {}
    for k, v in labelled.items():
        if v not in OK_HIDDEN:
            misses[v] = misses.get(v, 0) + 1
    hidden_now = {k: v for k, v in labelled.items() if now.get(k) in ("answered", "fyi")}
    pct = lambda d: (f"{sum(v in OK_HIDDEN for v in d.values())}/{len(d)}"
                     + (f" = {sum(v in OK_HIDDEN for v in d.values()) / len(d):.0%}" if d else ""))
    return {
        "labelled": len(labelled),
        "precision_as_labelled": pct(labelled),
        "misses": misses,
        "precision_now": pct(hidden_now),
        "misses_now_shown": sum(1 for k, v in labelled.items() if v not in OK_HIDDEN and now.get(k) in ("needs", "followup")),
        "done_now_shown": sum(1 for k, v in labelled.items() if v in OK_HIDDEN and now.get(k) in ("needs", "followup")),
        "now_by_state": {s: sum(1 for k in labelled if now.get(k) == s) for s in ("needs", "followup", "answered", "fyi")},
        "no_longer_in_window": sum(1 for k in labelled if k not in now),
    }


def write_samples(all_cards: list, n: int, me: str) -> str:
    """A mix of unhandled and handled cards as plain text, for someone who hasn't seen the chat."""
    # What still needs you first, then the rest taking one of each label in turn (question, request...).
    pick = [c for c in all_cards if c["handled"] is None][:n]
    by_label = {}
    for c in all_cards:
        if c["handled"] is not None:
            by_label.setdefault(c["label"], []).append(c)
    while len(pick) < n and any(by_label.values()):
        for lst in by_label.values():
            if lst and len(pick) < n:
                pick.append(lst.pop(0))
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, "mention-cards.md")
    lines = ["# Sample mention cards", "",
             f"For a cold reader: from each card alone, say (1) what is being asked of {me} and (2) whether they still",
             "owe anything. Write yes or no under each: was the card enough?", ""]
    for k, c in enumerate(pick, 1):
        state = f"handled: {c['handled']['how']}" if c["handled"] else "NEEDS YOU"
        lines += [f"## {k}. {c['label']} from {c['asker']} · {c['chat_name']} · {c['last_ping'][:16].replace('T', ' ')} · {state}", ""]
        if c["summary"]:
            lines += [f"> {c['summary']}", ""]
        for e in c["excerpt"]:
            if "gap" in e:
                lines.append(f"    … {e['gap']} more …")
            else:
                mark = {"ping": " ◀ ping", "ask": " ◀ ask", "reply": " ◀ your reply"}.get(e["role"], "")
                lines.append(f"    [{e['ts'][11:16]}] {e['author']}: {e['text']}{mark}".replace("\n", " / "))
        lines += ["", "Enough to understand? ____", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


SAMPLE_COLS = ["item", "chat", "ts", "author", "text", "rules", "for_me", "note"]


def mention_sample(st: Store, all_cards: list, prof: rules.Profile, org: str, n: int = 50, seed: int = 0) -> tuple:
    """n random messages in an org's chats (card window only) that contain your names, titles or committees
    anywhere, any case: looser than the rules, so misses show up. Fill in for_me (y/n); labels survive a re-export."""
    terms = [t.lower() for t in prof.names + prof.titles.get(org, []) + prof.committees.get(org, []) if t and len(t.strip()) > 1]
    since = (dt.datetime.now() - dt.timedelta(days=WINDOW_DAYS)).isoformat(timespec="minutes")
    hits = [r for r in st.q("SELECT id, chat_name, ts, author, text FROM item WHERE org = ? AND is_me = 0 AND ts >= ?"
                            " AND text IS NOT NULL ORDER BY id", (org, since))
            if any(t in r["text"].lower() for t in terms)]
    path = os.path.join(REPORTS, f"mention-sample-{org}.csv")
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            old = {r["item"]: r for r in csv.DictReader(f)}
    keep = [r for r in hits if str(r["id"]) in old]  # a re-export keeps the same messages and your labels
    rest = [r for r in hits if str(r["id"]) not in old]
    random.Random(seed).shuffle(rest)
    pick = sorted(keep + rest[: max(0, n - len(keep))], key=lambda r: r["id"])
    state = {i: c["state"] for c in all_cards for i in c["ping_items"]}
    os.makedirs(REPORTS, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, SAMPLE_COLS)
        w.writeheader()
        for r in pick:
            prev = old.get(str(r["id"]), {})
            w.writerow({"item": r["id"], "chat": r["chat_name"], "ts": r["ts"], "author": r["author"], "text": _flat(r["text"]),
                        "rules": state.get(r["id"], "no card"), "for_me": prev.get("for_me", ""), "note": prev.get("note", "")})
    return path, len(pick), len(hits)


def mention_score(all_cards: list, org: str) -> dict:
    """From your for_me labels: messages meant for you that today's rules miss, and ones they wrongly put in front of you."""
    path = os.path.join(REPORTS, f"mention-sample-{org}.csv")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    state = {i: c["state"] for c in all_cards for i in c["ping_items"]}
    lab = [(r, (r.get("for_me") or "").strip().lower()[:1]) for r in rows]
    yes = [r for r, v in lab if v == "y"]
    no = [r for r, v in lab if v == "n"]
    st = lambda r: state.get(int(r["item"]), "no card")
    return {
        "labelled": len(yes) + len(no),
        "for_me": len(yes),
        "missed_no_card": [r["item"] for r in yes if st(r) == "no card"],
        "for_me_by_state": {s: sum(1 for r in yes if st(r) == s) for s in ("needs", "followup", "answered", "fyi", "no card")},
        "not_for_me_but_shown": [r["item"] for r in no if st(r) in ("needs", "followup")],
    }
