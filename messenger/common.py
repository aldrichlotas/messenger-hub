"""Shared output format for Messenger chat exports (used by scrape.py, import_export.py and hub.py).

An export folder holds:
  messages.jsonl   one message per line (id, ts, sender, text, reply_to, reactions, attachments...)
  transcript.md    the whole chat, oldest first, split into sessions, for an agent to read
  attachments/     downloaded photos, videos and files, referenced from both of the above
  meta.json        chat title, participants, source, export time
"""

import collections
import datetime as dt
import hashlib
import json
import os
import re

SESSION_GAP = dt.timedelta(hours=3)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_time_label(label: str, now: dt.datetime):
    """Best-effort parse of Messenger's separator labels ("Today at 3:14 PM", "Mon 9:02",
    "Oct 3, 2026, 4:00 PM", "10/3/26 16:00"). Returns a naive local datetime or None."""
    s = label.replace(" ", " ").replace("\xa0", " ").replace(" at ", " ").replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*([AaPp]\.?[Mm]\.?)?", s)
    hour = minute = 0
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        ampm = (tm.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        date_part = (s[: tm.start()] + s[tm.end() :]).strip().lower()
    else:
        date_part = s.lower()

    today = now.date()
    day = None
    if date_part in ("", "today"):
        day = today if tm else None
    elif date_part == "yesterday":
        day = today - dt.timedelta(days=1)
    elif date_part[:3] in WEEKDAYS and not re.search(r"\d", date_part):
        back = (today.weekday() - WEEKDAYS.index(date_part[:3])) % 7 or 7
        day = today - dt.timedelta(days=back)
    else:
        m = re.match(r"([a-z]+)\.? (\d{1,2})(?: (\d{4}))?", date_part) or re.match(
            r"(\d{1,2}) ([a-z]+)\.?(?: (\d{4}))?", date_part
        )
        if m:
            a, b, year = m.groups()
            mon, d = (a, b) if a.isalpha() else (b, a)
            if mon[:3] in MONTHS:
                y = int(year) if year else today.year
                try:
                    day = dt.date(y, MONTHS.index(mon[:3]) + 1, int(d))
                    if not year and day > today:
                        day = day.replace(year=y - 1)
                except ValueError:
                    day = None
        else:
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_part)
            if m:
                mo, d, y = (int(x) for x in m.groups())
                y = y + 2000 if y < 100 else y
                try:
                    day = dt.date(y, mo, d)
                except ValueError:
                    day = None
    if day is None:
        return None
    return dt.datetime(day.year, day.month, day.day, hour, minute)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().rstrip("…").rstrip(".")).lower()


def message_key(m: dict) -> str:
    """Content fingerprint used to line up a new scrape with an existing export."""
    if m.get("mid"):
        return "mid:" + m["mid"]
    kinds = ",".join(sorted(a["type"] for a in m.get("attachments", [])))
    blob = "|".join([m.get("sender") or "", norm(m.get("text")), norm(m.get("reply_quote")), kinds])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def media_key(url: str) -> str:
    """Facebook serves one photo at several sizes; the first two numbers of the file name identify it."""
    name = (url or "").split("?")[0].rsplit("/", 1)[-1]
    return "_".join(name.split("_")[:2])


def link_replies(messages: list) -> None:
    """Point each reply at the message it quotes: the most recent earlier message by the replied-to
    person whose text matches the quote or, for a quoted photo or video, whose attachment matches."""
    for i, m in enumerate(messages):
        if m.get("reply_to") or not (m.get("reply_quote") or m.get("reply_header")):
            continue
        quote = norm(m.get("reply_quote"))
        target = m.get("reply_target")
        mine = {media_key(a.get("url")): a for a in m.get("attachments", []) if a.get("url")}
        for strict in (True, False):
            for j in range(i - 1, -1, -1):
                o = messages[j]
                if strict and target and o.get("sender") != target:
                    continue
                t = norm(o.get("text"))
                if quote and t and (t == quote or t.startswith(quote) or quote.startswith(t)):
                    m["reply_to"] = o["id"]
                    break
                if not quote:
                    shared = [k for k in (media_key(a.get("url")) for a in o.get("attachments", [])) if k in mine]
                    if shared:
                        m["reply_to"] = o["id"]
                        for k in shared:
                            mine[k]["quoted"] = True  # the reply only shows the original's thumbnail
                        break
            if m.get("reply_to"):
                break


def assign_sessions(messages: list) -> None:
    """Group messages into conversation sessions: a new one after a 3 h gap or a day change."""
    session, last = 0, None
    for m in messages:
        ts = dt.datetime.fromisoformat(m["ts"]) if m.get("ts") else None
        if session == 0 or (ts and last and (ts - last > SESSION_GAP or ts.date() != last.date())):
            session += 1
        if ts:
            last = ts
        m["session"] = session


def bookmark(messages: list):
    """The newest exported message with a timestamp: where the next sync picks up."""
    return next((m for m in reversed(messages) if m.get("ts")), None)


def after_bookmark(fresh: list, existing: list) -> list:
    """The messages in `fresh` that are newer than the export's bookmark. Timestamps are only
    minute-precise, so messages in the bookmark's own minute are kept only if they aren't already
    exported (matched by message id or content)."""
    mark = bookmark(existing)
    if not mark:
        return merge(existing, fresh)[len(existing) :]
    cutoff = mark["ts"]
    # Counted, not a set: two separate "G" messages in the same minute are both real.
    known = collections.Counter(message_key(m) for m in existing if not m.get("ts") or m["ts"] >= cutoff)
    new = []
    for m in fresh:
        if m.get("ts") and m["ts"] < cutoff:
            continue
        if not m.get("ts") or m["ts"] == cutoff:
            key = message_key(m)
            if known[key] > 0:
                known[key] -= 1
                continue
        new.append(m)
    return new


def drop_repeats(messages: list, window: int = 40) -> list:
    """Remove rows a scrape captured twice: same sender, text, attachments and minute as a row up to
    `window` rows earlier. Back-to-back identical messages ("lol", "lol") are kept: those are real."""
    out, recent, prev = [], [], None
    for m in messages:
        key = None
        if m.get("kind") == "message" and m.get("ts"):
            kinds = ",".join(sorted(a.get("type", "") for a in m.get("attachments", [])))
            key = (m.get("sender"), norm(m.get("text")), kinds, m["ts"][:16])
            if (norm(m.get("text")) or kinds) and key != prev and key in recent:
                prev = key  # "A B A B": the second B follows a dropped A, and is a repeat too
                continue
        prev = key
        recent = (recent + [key])[-window:]
        out.append(m)
    return out


def merge(existing: list, fresh: list) -> list:
    """Append the messages in `fresh` that come after the end of `existing`.
    Lines the two up on the longest run of matching keys; if they don't overlap, the longer wins."""
    if not existing:
        return fresh
    old_keys = [message_key(m) for m in existing]
    new_keys = [message_key(m) for m in fresh]
    for run in (8, 5, 3, 2, 1):
        if len(old_keys) < run:
            continue
        tail = old_keys[-run:]
        for start in range(len(new_keys) - run, -1, -1):
            if new_keys[start : start + run] == tail:
                return existing + fresh[start + run :]
    return fresh if len(fresh) >= len(existing) else existing


def load_messages(out_dir: str) -> list:
    path = os.path.join(out_dir, "messages.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _clip(text: str, n: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def write_export(out_dir: str, meta: dict, messages: list) -> None:
    for i, m in enumerate(messages, 1):
        m["id"] = i
        m.setdefault("reply_to", None)
    # Re-link from scratch: ids are renumbered on every write.
    for m in messages:
        if m.get("reply_quote") or m.get("reply_header"):
            m["reply_to"] = None
    link_replies(messages)
    assign_sessions(messages)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "messages.jsonl"), "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    senders = sorted({m["sender"] for m in messages if m.get("sender") and m["kind"] == "message"})
    meta = dict(meta)
    meta["participants"] = sorted(set(meta.get("participants") or []) | set(senders))
    meta["message_count"] = sum(1 for m in messages if m["kind"] == "message")
    meta["attachment_count"] = sum(1 for m in messages for a in m.get("attachments", []) if not a.get("quoted"))
    stamps = [m["ts"] for m in messages if m.get("ts")]
    meta["first"], meta["last"] = (stamps[0], stamps[-1]) if stamps else (None, None)
    meta["exported_at"] = dt.datetime.now().isoformat(timespec="seconds")
    mark = bookmark(messages)
    meta["last_exported"] = (
        {
            "id": mark["id"],
            "ts": mark["ts"],
            "mid": mark.get("mid"),
            "sender": mark.get("sender"),
            "text": _clip(mark.get("text"), 60),
        }
        if mark
        else None
    )
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    by_id = {m["id"]: m for m in messages}
    lines = [
        f"# {meta.get('title') or 'Messenger chat'}",
        "",
        f"- Participants: {', '.join(meta['participants']) or 'unknown'}",
        f"- Messages: {meta['message_count']} · attachments: {meta['attachment_count']}"
        f" · {meta['first'] or '?'} → {meta['last'] or '?'}",
        f"- Source: {meta.get('source')} · exported {meta['exported_at']}",
        "",
        "How to read this: messages are oldest first, grouped into sessions (a new session starts after"
        " a 3-hour gap or a new day). Each line is `[#id time] Sender: text`. `↪ #n` means the message"
        " replies to message #n (the quoted text or [photo] follows). `📎` lines are files in"
        " `attachments/`, `🔗` are shared links, `⟡` are reactions, and `·` lines are chat events."
        " Full data for every message is in `messages.jsonl` under the same id.",
        "",
    ]
    current = None
    for m in messages:
        if m["session"] != current:
            current = m["session"]
            count = sum(1 for x in messages if x["session"] == current and x["kind"] == "message")
            start = m.get("ts") or m.get("time_label") or "time unknown"
            lines += ["", f"## Session {current} · {start} · {count} message{'s' if count != 1 else ''}", ""]
        lines += format_message(m, by_id)
    with open(os.path.join(out_dir, "transcript.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def format_message(m: dict, by_id: dict, prefix: str = "") -> list:
    """One message as transcript lines: `[#id hh:mm] **Sender**: text` plus reply, attachment, link
    and reaction lines. `prefix` (e.g. a chat slug) is put in front of every #id."""
    lines = []
    clock = m["ts"][11:16] if m.get("ts") else ""
    tag = f"[{prefix}#{m['id']}{' ' + clock if clock else ''}]"
    if m["kind"] == "event":
        return [f"{tag} · {_clip(m.get('text') or m.get('raw'), 200)}"]
    body = (m.get("text") or "").replace("\n", "\n    ")
    lines.append(f"{tag} **{m.get('sender') or '?'}**: {body}{' _(edited)_' if m.get('edited') else ''}")
    if m.get("reply_quote") or m.get("reply_to") or m.get("reply_header"):
        target = by_id.get(m.get("reply_to"))
        who = f"{target['sender']}: " if target else (f"{m['reply_target']}: " if m.get("reply_target") else "")
        ref = f"{prefix}#{target['id']}" if target else "(original not in export)"
        quoted = m.get("reply_quote") or (target or {}).get("text")
        media = [a["type"] for a in (target or m).get("attachments", []) if target or a.get("quoted")]
        shown = f'"{_clip(quoted)}"' if quoted else ""
        if media:
            shown = (shown + " " if shown else "") + " ".join(f"[{t}]" for t in media)
        lines.append(f"    ↪ {ref} {who}{shown or '(quoted message)'}")
    for a in m.get("attachments", []):
        if a.get("quoted"):
            continue
        where = a.get("path") or a.get("url") or "(not downloaded)"
        lines.append(f"    📎 {a['type']}: {where}{' (' + a['name'] + ')' if a.get('name') else ''}")
    for link in m.get("links", []):
        lines.append(f"    🔗 {link}")
    if m.get("reactions"):
        lines.append(f"    ⟡ {'; '.join(m['reactions'])}")
    return lines


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "chat"
