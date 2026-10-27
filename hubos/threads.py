"""Conversations and mention cards. No tokens (the one-line summaries are optional, see summarise()).

A busy group chat interleaves several conversations. They're split with the usual chat
disentanglement signals: a silence of GAP starts a new conversation, and a reply joins the
conversation of the message it answers (up to REPLY_REACH back). So a ping lands with the
messages that explain it.

Each conversation that pings you becomes one card: what kind of ask it is, whether you already
handled it, and the few messages that explain it.

    py -3.10 hub.py threads --report     check the split and the handled flags; writes 10 sample cards
"""

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

from . import rules
from .store import ROOT, Store, now_iso

GAP = dt.timedelta(minutes=30)  # silence that ends a conversation
REPLY_REACH = dt.timedelta(hours=24)  # a reply rejoins its conversation up to this long after it went quiet
BURST = dt.timedelta(minutes=5)  # one sender's messages this close together are one message
REPLIED = dt.timedelta(minutes=60)  # you posting this soon after a ping counts as answering it
WINDOW_DAYS = 30
REPORTS = os.path.join(ROOT, "data", "reports")

QUESTION = re.compile(
    r"\?|\b(ano|anong|sino|saan|nasaan|kailan|bakit|paano|pano|pwede|puwede|can you|could you|will you|would you|are you|"
    r"r u|do you|did you|have you|what|when|where|who|how|why|which|tapos na|ready na)\b",
    re.I,
)
ASK = re.compile(
    r"\b(pls|plz|please|paki\w*|pa-\w+|need you|help|check|review|send|upload|post|fix|join|gawa|gawin|ikaw na|mo na|turn on)\b",
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
    rx = rules._words_rx(prof.names)
    text = text or ""
    return re.sub(r"@\s*", "", rx.sub("", text) if rx else text).strip()


def _line(m: dict, role: str = "") -> dict:
    t = (m["text"] or "").strip() or "(photo or file)"
    return {"author": m["author"], "is_me": bool(m["is_me"]), "text": t if len(t) <= 400 else t[:399] + "…",
            "ts": m["ts"], "ref": m["ref"], "role": role}


def _dedupe(lines: list) -> list:
    """The export sometimes has a message twice in a row (a photo and its caption): show it once."""
    out = []
    for ln in lines:
        prev = out[-1] if out else None
        # The scraper sometimes repeats a few messages (A B A B): same sender, text and minute = one message.
        twin = "gap" not in ln and next((p for p in out[-4:] if "gap" not in p and (p["author"], p["text"], p["ts"][:16])
                                         == (ln["author"], ln["text"], ln["ts"][:16])), None)
        if twin:
            twin["role"] = twin["role"] or ln["role"]
            continue
        if prev and "gap" not in ln and "gap" not in prev and ln["text"] == "(photo or file)" and prev["author"] == ln["author"] \
                and prev["ts"][:16] == ln["ts"][:16]:
            continue  # the photo that went with the caption above
        out.append(ln)
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
    focus = open_pings[0] if open_pings else pings[-1]
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

    handled = None
    if not open_pings:
        r = my_reply_after(pings[-1])
        handled = {"how": "you replied", "ts": r["ts"]}
    elif label == "question" and mentions.get(focus["id"], {}).get("strength", 3) < 3 and not focus["reply_to_me"]:
        other = next((m for m in msgs[i + 1 :] if not m["is_me"] and m["author"] != focus["author"]
                      and (str(m.get("reply_ext")) == str(focus["ext_id"]))), None)
        if other:
            handled = {"how": f"answered by {other['author']}", "ts": other["ts"]}
    if not handled and label in ("banter", "fyi"):
        handled = {"how": "nothing asked", "ts": focus["ts"]}

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
        "due": due,
        "handled": handled,
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
    }
    out["samples_file"] = write_samples(all_cards, samples, me)
    return out


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
