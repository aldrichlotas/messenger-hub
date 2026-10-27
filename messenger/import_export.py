"""Convert a chat from Facebook's official "Download your information" export into the same
agent-ready format as scrape.py.

    py messenger/import_export.py <chat folder> [--out DIR] [--me NAME]

<chat folder> is the unzipped folder holding message_1.json (message_2.json, ...), for example
  .../your_facebook_activity/messages/inbox/ourgroupchat_123456789/
  .../your_facebook_activity/messages/e2ee_cutover/ourgroupchat_123456789/
Request the export (JSON format) at Accounts Center -> Your information and permissions ->
Download your information. End-to-end encrypted chats may instead need Messenger's
"Download secure storage data" (Settings -> Privacy & safety -> End-to-end encrypted chats ->
Message storage); point this script at whatever folder holds that chat's JSON files.

Unlike the web scrape this has exact timestamps and every attachment at full size, but no reply
links (Facebook doesn't include them in this export).
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fix(s):
    """Facebook writes UTF-8 bytes as \\u00XX escapes (mojibake); undo that."""
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def find_file(uri: str, chat_dir: str):
    uri = uri.replace("\\", "/")
    d = chat_dir
    for _ in range(8):
        candidate = os.path.join(d, uri)
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    hits = glob.glob(os.path.join(chat_dir, "**", os.path.basename(uri)), recursive=True)
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat_dir")
    ap.add_argument("--out", help="output folder (default data/chats/<chat-name>-export)")
    ap.add_argument("--me", help="your name as it appears in the export, to label as yourself")
    args = ap.parse_args()

    parts = sorted(glob.glob(os.path.join(args.chat_dir, "message_*.json")))
    if not parts:
        sys.exit(f"No message_*.json files in {args.chat_dir}")
    raw, title, participants = [], None, set()
    for path in parts:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        title = title or fix(data.get("title"))
        participants |= {fix(p.get("name") if isinstance(p, dict) else p) for p in data.get("participants", [])}
        raw += data.get("messages", [])

    out_dir = args.out or os.path.join(ROOT, "data", "chats", common.slugify(title or "chat") + "-export")
    att_dir = os.path.join(out_dir, "attachments")
    os.makedirs(att_dir, exist_ok=True)

    messages, missing = [], 0
    for r in raw:
        stamp = r.get("timestamp_ms") or r.get("timestamp")
        if stamp and stamp < 10**11:  # seconds, not milliseconds
            stamp *= 1000
        ts = dt.datetime.fromtimestamp(stamp / 1000) if stamp else None
        sender = fix(r.get("sender_name") or r.get("senderName"))
        if args.me and sender == args.me:
            sender = "You"
        text = fix(r.get("content") or r.get("text") or "")
        if r.get("is_unsent") or r.get("isUnsent"):
            text = text or "(message unsent)"

        attachments = []
        groups = [
            ("image", r.get("photos")),
            ("video", r.get("videos")),
            ("audio", r.get("audio_files")),
            ("file", r.get("files")),
            ("gif", r.get("gifs")),
            ("sticker", [r["sticker"]] if r.get("sticker") else None),
            ("media", r.get("media")),
        ]
        for kind, items in groups:
            for item in items or []:
                uri = item.get("uri") if isinstance(item, dict) else item
                if not uri:
                    continue
                if uri.startswith("http"):
                    attachments.append({"type": kind, "url": uri})
                    continue
                src = find_file(uri, args.chat_dir)
                if not src:
                    missing += 1
                    attachments.append({"type": kind, "name": os.path.basename(uri), "path": None})
                    continue
                dest = os.path.join(att_dir, os.path.basename(src))
                if not os.path.exists(dest):
                    shutil.copy2(src, dest)
                attachments.append({"type": kind, "path": f"attachments/{os.path.basename(src)}"})

        links = []
        share = r.get("share") or {}
        if share.get("link"):
            links.append(share["link"])
        if share.get("share_text") and not text:
            text = fix(share["share_text"])

        reactions = [f"{fix(x.get('reaction'))} by {fix(x.get('actor'))}" for x in r.get("reactions") or []]
        messages.append(
            {
                "kind": "message",
                "ts": ts.isoformat(timespec="seconds") if ts else None,
                "time_label": None,
                "sender": sender,
                "text": text,
                "reply_quote": "",
                "reactions": reactions,
                "edited": False,
                "links": links,
                "attachments": attachments,
            }
        )

    messages.sort(key=lambda m: m["ts"] or "")
    meta = {
        "title": title,
        "source": f"Facebook data export ({os.path.abspath(args.chat_dir)})",
        "participants": sorted({"You" if p == args.me else p for p in participants if p}),
    }
    common.write_export(out_dir, meta, messages)
    print(f"Done: {len(messages)} messages -> {out_dir}")
    if missing:
        print(f"{missing} attachments referenced in the JSON weren't found next to it.")


if __name__ == "__main__":
    main()
