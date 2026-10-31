"""The org vault: built from made-up chats, no model calls and no network."""

import filecmp
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import process, vault  # noqa: E402
from hubos.store import Store  # noqa: E402

CFG = {
    "me": "Aldrich",
    "profile": {"names": ["Aldrich"], "not_me": ["Aldrich Ty"]},
    "orgs": {"ecg": {"name": "ECG", "my_title": "DCCO", "aliases": ["Deputy CCO"], "committees": ["Media"], "above": ["Sam Reyes"],
                     "chats": [{"slug": "eb", "name": "[ECG] EB", "enabled": True}]}},
}
MSGS = [
    ("1", "2026-10-01T09:00", "Sam Reyes", "pubmat for the GA, deadline Friday 5pm"),
    ("2", "2026-10-01T09:01", "Antoinette Dellosa", '[PROMAN VD] Antoi voted for "I will attend." in the poll. View poll'),
    ("3", "2026-10-02T09:00", "Aldrich", "ok noted"),
    ("4", "2026-10-03T09:00", "Aldrich Ty", "We decided to move the GA to the next term"),
    ("5", "2026-10-03T09:05", "Sam", "ok"),
    ("6", "2026-10-03T09:06", ": Helloo", "scraper leftover"),
    ("7", "2026-10-09T09:06", "Antoinette Dellosa", '[PROMAN Direk] Antoi voted for "Yes" in the poll. View poll'),
    ("8", "2026-10-10T09:00", "[HR VD] Pisha", '[HR VD] Pisha voted for "Yes" in the poll. View poll'),
    ("9", "2026-10-11T09:00", "Sam Reyes", "for the pubmat"),
    ("10", "2026-10-11T09:02", "Sam Reyes", "@Aldrich ikaw na"),
]


class Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = Store(os.path.join(self.tmp.name, "hub.db"))
        self.addCleanup(self.st.db.close)
        os.makedirs(os.path.join(self.tmp.name, "eb"))
        with open(os.path.join(self.tmp.name, "eb", "messages.jsonl"), "w", encoding="utf-8") as f:
            for i, ts, who, text in MSGS:
                f.write(json.dumps({"kind": "message", "id": i, "ts": ts, "sender": who, "text": text}) + "\n")
        with mock.patch.object(process, "CHATS", self.tmp.name):
            process.ingest(self.st, CFG, force=True)
        self.out = os.path.join(self.tmp.name, "vault", "Hub")
        os.makedirs(os.path.dirname(self.out))

    def build(self, out=None):
        return vault.build(self.st, CFG, out or self.out)

    def read(self, *path):
        with open(os.path.join(self.out, *path), encoding="utf-8") as f:
            return f.read()

    def test_a_role_has_its_dates_and_its_message(self):
        self.build()
        p = self.read("Extracted", "People", "Antoinette Dellosa.md")
        self.assertIn("PROMAN VD (shown as Antoi), 2026-10-01 to 2026-10-01: eb#2", p)
        self.assertIn("PROMAN Direk (shown as Antoi), 2026-10-09 to 2026-10-09: eb#7", p)

    def test_a_namesake_is_marked_not_you(self):
        self.build()
        self.assertIn("**Not you.**", self.read("Extracted", "People", "Aldrich Ty.md"))

    def test_you_and_scraper_leftovers_get_no_person_note(self):
        self.build()
        names = os.listdir(os.path.join(self.out, "Extracted", "People"))
        self.assertNotIn("Aldrich.md", names)
        self.assertFalse([n for n in names if "Helloo" in n])

    def test_people_are_only_listed_as_maybe_the_same(self):
        self.build()
        self.assertTrue(os.path.exists(os.path.join(self.out, "Extracted", "People", "Sam.md")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "Extracted", "People", "Sam Reyes.md")))
        self.assertIn("[[Sam]], [[Sam Reyes]]", self.read("Review", "People to check.md"))

    def test_guesses_are_kept_apart_from_what_you_accepted(self):
        self.build()
        self.assertTrue(os.listdir(os.path.join(self.out, "Inferred", "Deadlines")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "Extracted", "Deadlines")))
        self.st.run("UPDATE entry SET review = 'accepted'")
        self.build()
        self.assertTrue(os.listdir(os.path.join(self.out, "Extracted", "Deadlines")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "Inferred", "Deadlines")))

    def test_a_decision_is_inferred_and_cites_its_message(self):
        self.build()
        d = self.read("Inferred", "Decisions", "decision eb 4.md")
        self.assertIn('"basis": ', d.replace("basis: ", '"basis": '))
        self.assertIn('source: ["eb#4"]', d)

    def test_an_entry_read_with_earlier_messages_cites_them_too(self):
        item = self.st.one("SELECT id FROM item WHERE ref = 'eb#10'")["id"]
        e = {"ref": "eb#10", "item_id": item, "reasons": "read with the messages around it"}
        self.assertEqual(vault.sources(self.st, e), ["eb#10", "eb#9"])
        self.assertEqual(vault.sources(self.st, {**e, "reasons": "a deadline"}), ["eb#10"])

    def test_a_role_with_no_full_name_is_listed_to_check(self):
        self.build()
        self.assertIn("- HR VD (shown as Pisha): eb#8", self.read("Review", "People to check.md"))
        self.assertFalse([n for n in os.listdir(os.path.join(self.out, "Extracted", "People")) if "Pisha" in n])

    def test_rejected_entries_are_left_out(self):
        self.st.run("UPDATE entry SET review = 'rejected'")
        self.assertEqual(self.build().get("inferred", 0), 0)

    def test_every_person_and_entry_note_cites_a_message_that_exists(self):
        self.build()
        refs = {r["ref"] for r in self.st.q("SELECT ref FROM item")}
        for folder, _, names in os.walk(self.out):
            for n in names:
                if n.endswith(".md") and "Orgs" not in folder and "Review" not in folder:
                    with open(os.path.join(folder, n), encoding="utf-8") as f:
                        line = next(x for x in f if x.startswith("source:"))
                    self.assertLessEqual(set(json.loads(line[len("source:"):])), refs, n)

    def test_rebuilding_gives_identical_files(self):
        self.build()
        other = os.path.join(self.tmp.name, "again", "Hub")
        os.makedirs(os.path.dirname(other))
        self.build(other)
        cmp = filecmp.dircmp(self.out, other)

        def same(c):
            return not (c.left_only or c.right_only or c.diff_files) and all(same(s) for s in c.subdirs.values())

        self.assertTrue(same(cmp))

    def test_a_rebuild_removes_what_no_longer_applies_and_leaves_the_rest_alone(self):
        self.build()
        stale = os.path.join(self.out, "Inferred", "Old", "gone.md")
        os.makedirs(os.path.dirname(stale))
        with open(stale, "w", encoding="utf-8") as f:
            f.write("old")
        kept = os.path.join(self.out, "Review", "People to check.md")
        os.utime(kept, (1000, 1000))
        self.build()
        self.assertFalse(os.path.exists(os.path.dirname(stale)))
        self.assertEqual(os.path.getmtime(kept), 1000)

    def test_it_never_touches_a_folder_it_didnt_write(self):
        os.makedirs(self.out)
        with open(os.path.join(self.out, "mine.md"), "w", encoding="utf-8") as f:
            f.write("my notes")
        with self.assertRaises(ValueError):
            self.build()
        self.assertTrue(os.path.exists(os.path.join(self.out, "mine.md")))

    def test_your_notes_beside_it_survive_a_rebuild(self):
        mine = os.path.join(os.path.dirname(self.out), "My notes.md")
        with open(mine, "w", encoding="utf-8") as f:
            f.write("my notes")
        self.build()
        self.build()
        self.assertTrue(os.path.exists(mine))

    def test_it_uses_no_network(self):
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")):
            self.build()


if __name__ == "__main__":
    unittest.main()
