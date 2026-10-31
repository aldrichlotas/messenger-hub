"""The org vault: Markdown notes for Obsidian, built only from the store and orgs.json.

    py -3.10 hub.py vault [--out FOLDER]

No model calls and no network. The store stays the source of truth: everything under Hub/ is rebuilt
from it each run, so deleting the folder loses nothing, and the same data gives the same files.
Your own notes live elsewhere in the vault: nothing outside Hub/ is touched.

Hub/Extracted/  read straight from the data: org settings, who sent what, role tags, entries you accepted
Hub/Inferred/   the rules' guesses: entries nobody reviewed, and decisions found by cue words
Hub/Review/     people who might be the same person: merged only on an exact name match
"""

import collections
import json
import os
import re

from . import process, rules
from .store import Store

OUT = os.path.join(os.path.expanduser("~"), "Documents", "Obsidian Vault", "Hub")
MARK = ".hub-generated"  # a folder without this isn't ours, and never gets deleted
NOT_A_NAME = re.compile(r"^[\[:\d]")  # scraper leftovers in the sender column: "[CREA Direk] Kichi", ": Helloo", "2026, 4:38 PM,"
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]+')


def file_name(text: str) -> str:
    return UNSAFE.sub(" ", text).strip(" .")[:80] or "untitled"


def is_person(name) -> bool:
    return bool(name and name.strip()) and not NOT_A_NAME.match(name.strip()) and len(name) <= 40


def note(fields: dict, *lines) -> str:
    head = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fields.items() if v not in (None, [], ""))
    return f"---\n{head}---\n\n" + "\n".join(lines).rstrip() + "\n"


class Names:
    """Note names, unique ignoring case: Windows can't hold both "Kim.md" and "KIM.md"."""

    def __init__(self):
        self.seen = {}

    def get(self, key: str, text: str) -> str:
        name, n = file_name(text), 1
        while True:
            label = name if n == 1 else f"{name} {n}"
            if self.seen.setdefault(label.casefold(), key) == key:
                return label
            n += 1


def people(st: Store, cfg: dict) -> dict:
    """name -> who they are in the chats, from the sender column and their own role tags."""
    out = {}
    orgs = cfg.get("orgs", {})
    rows = st.q("SELECT author, org, channel, chat_name, is_me, ts, ref, text FROM item WHERE source = 'messenger' AND author IS NOT NULL ORDER BY ts, id")
    for r in rows:
        name = r["author"].strip()
        if not is_person(name) or r["is_me"]:
            continue
        p = out.setdefault(name, {"orgs": set(), "chats": collections.Counter(), "first": r["ts"], "last": r["ts"], "first_ref": r["ref"], "roles": {}})
        p["orgs"].add(orgs.get(r["org"], {}).get("name") or r["org"])
        p["chats"][r["chat_name"] or r["channel"]] += 1
        p["last"] = r["ts"]
        tag = rules.role_tag(r["text"])
        if tag:
            role = p["roles"].setdefault(tag[0], {"nick": tag[1], "first": r["ts"], "last": r["ts"], "ref": r["ref"]})
            role["last"] = r["ts"]
    return out


def maybe_same(names: list) -> list:
    """Groups of names that share a first name. Listed for you to check, never merged."""
    groups = collections.defaultdict(list)
    for n in names:
        groups[n.split()[0].casefold()].append(n)
    return [sorted(g) for _, g in sorted(groups.items()) if len(g) > 1]


def decisions(st: Store) -> list:
    rows = st.q("SELECT org, ts, ref, author, text FROM item WHERE source = 'messenger' AND org IS NOT NULL ORDER BY ts, id")
    return [r for r in rows if rules.decision(r["text"])]


def sources(st: Store, e: dict) -> list:
    """An entry's message, plus the ones it was read with: its title can come from those."""
    if "read with" not in (e["reasons"] or ""):
        return [e["ref"]]
    item = dict(st.one("SELECT * FROM item WHERE id = ?", (e["item_id"],)))
    ts = process._ts(item["ts"])
    if not ts:
        return [e["ref"]]
    burst, parent = process._context(st, e["item_id"], item, ts)
    return [e["ref"]] + [c["ref"] for c in [parent] + burst if c]


def nameless_roles(st: Store) -> dict:
    """(role, nickname) -> message, for senders the chats give no full name: "[PROMAN Direk] Aya"."""
    out = {}
    for r in st.q("SELECT author, ref, text FROM item WHERE source = 'messenger' AND is_me = 0 AND text LIKE '[%' ORDER BY ts, id"):
        tag = rules.role_tag(r["text"])
        if tag and not is_person(r["author"]):
            out.setdefault(tag, r["ref"])
    return out


def write(files: dict, out: str) -> None:
    """Make out match files, only where something changed: Obsidian watches the folder, so it can't be swapped whole."""
    want = set()
    for rel, text in sorted(files.items()):
        path = os.path.join(out, *rel.split("/"))
        want.add(os.path.normcase(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = text.encode("utf-8")
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read() == data:
                    continue
        with open(path, "wb") as f:
            f.write(data)
    for folder, _, names in os.walk(out, topdown=False):
        for n in names:
            if os.path.normcase(os.path.join(folder, n)) not in want:
                os.remove(os.path.join(folder, n))
        if folder != out and not os.listdir(folder):
            os.rmdir(folder)


def build(st: Store, cfg: dict, out: str = OUT) -> dict:
    """Rebuild the vault's generated folder. All of it is worked out before the disk is touched."""
    if os.path.exists(out) and os.listdir(out) and not os.path.exists(os.path.join(out, MARK)):
        raise ValueError(f"{out} has files the hub didn't write: choose an empty folder.")
    files = {MARK: "Everything in this folder is rebuilt by hub.py vault. Keep your own notes outside it.\n"}
    names = Names()
    count = collections.Counter()
    orgs = cfg.get("orgs", {})
    prof = cfg.get("profile") or {}
    not_me = set(prof.get("not_me") or [])
    who = people(st, cfg)
    links = {n: names.get("person:" + n, n) for n in sorted(who)}

    def link(name: str) -> str:
        return f"[[{links[name]}]]" if name in links else name

    for key, org in sorted(orgs.items()):
        label = org.get("name") or key
        rows = st.q("SELECT chat_name, channel, COUNT(*) AS n FROM item WHERE org = ? GROUP BY channel ORDER BY channel", (key,))
        files[f"Extracted/Orgs/{names.get('org:' + key, label)}.md"] = note(
            {"type": "org", "basis": "extracted", "source": "orgs.json", "key": key, "my_title": org.get("my_title"),
             "my_title_aliases": org.get("aliases"), "my_committees": org.get("committees"), "above_me": org.get("above")},
            f"# {label}", "",
            f"My title: {org.get('my_title') or 'not set'}", "",
            "People above me: " + (", ".join(link(n) for n in org.get("above") or []) or "not set"), "",
            "My committees: " + (", ".join(org.get("committees") or []) or "not set"), "",
            "## Chats", *[f"- {r['chat_name'] or r['channel']}: {r['n']} messages" for r in rows],
        )
        count["orgs"] += 1

    for name, p in sorted(who.items()):
        roles = sorted(p["roles"].items(), key=lambda kv: kv[1]["first"])
        cites = [p["first_ref"]] + [r["ref"] for _, r in roles]
        lines = [f"# {name}", ""]
        if name in not_me:
            lines += ["**Not you.** A namesake listed under not_me in your profile.", ""]
        lines += [f"Seen {p['first'][:10]} to {p['last'][:10]}, in {', '.join(sorted(p['orgs']))}.", ""]
        if roles:
            lines += ["## Roles", *[f"- {t} (shown as {r['nick']}), {r['first'][:10]} to {r['last'][:10]}: {r['ref']}" for t, r in roles], ""]
        lines += ["## Chats", *[f"- {c}: {n} messages" for c, n in sorted(p["chats"].items())]]
        files[f"Extracted/People/{links[name]}.md"] = note(
            {"type": "person", "basis": "extracted", "orgs": sorted(p["orgs"]), "nicknames": sorted({r["nick"] for _, r in roles}),
             "roles": [t for t, _ in roles], "not_me": True if name in not_me else None, "source": sorted(set(cites))},
            *lines,
        )
        count["people"] += 1

    for e in st.q("SELECT e.*, i.ts AS said FROM entry e LEFT JOIN item i ON i.id = e.item_id WHERE e.review != 'rejected' AND e.ref IS NOT NULL ORDER BY e.id"):
        basis = "extracted" if e["review"] == "accepted" else "inferred"
        label = names.get(f"entry:{e['id']}", f"{e['kind']} {e['ref']}")
        folder = "Extracted" if basis == "extracted" else "Inferred"
        cites = sources(st, e)
        files[f"{folder}/{e['kind'].title()}s/{label}.md"] = note(
            {"type": e["kind"], "basis": basis, "org": orgs.get(e["org"], {}).get("name") or e["org"], "due": e["due"], "status": e["status"],
             "review": e["review"], "said": e["said"], "source": cites},
            f"# {e['title']}", "", f"Source: {', '.join(cites)}",
        )
        count[basis] += 1

    for d in decisions(st):
        label = names.get(f"decision:{d['ref']}", f"decision {d['ref']}")
        files[f"Inferred/Decisions/{label}.md"] = note(
            {"type": "decision", "basis": "inferred", "org": orgs.get(d["org"], {}).get("name") or d["org"], "said": d["ts"],
             "said_by": d["author"], "source": [d["ref"]]},
            f"# {rules.clean_title(d['text'], 140)}", "", f"Source: {d['ref']}", "",
            "Found by a cue word, so check the message before relying on it. A later message may have changed this.",
        )
        count["decisions"] += 1

    groups = maybe_same(sorted(who))
    files["Review/People to check.md"] = note(
        {"type": "review", "basis": "extracted", "source": "item.author"},
        "# People to check", "",
        "These names share a first name. They are kept separate until you say otherwise.", "",
        *[f"- {', '.join(link(n) for n in g)}" for g in groups], "",
        "## Roles with no full name", "",
        "The chats show these senders by nickname only, so they have no person note.", "",
        *[f"- {role} (shown as {nick}): {ref}" for (role, nick), ref in sorted(nameless_roles(st).items())],
    )
    write(files, out)
    return {**count, "to_check": len(groups), "files": len(files), "out": out}
