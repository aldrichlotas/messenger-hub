"""Mentions split from what's asked: bursts from one sender, and replies."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import process  # noqa: E402
from hubos.store import Store  # noqa: E402

CFG = {
    "me": "Aldrich",
    "profile": {"names": ["Aldrich"]},
    "orgs": {"ecg": {"name": "ECG", "chats": [{"slug": "eb", "name": "[ECG] EB", "enabled": True}],
                     "committees": ["Creatives"], "above": ["Sam"]}},
}
T0 = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(hours=1)


class SplitMentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "hub.db"))
        os.makedirs(os.path.join(self.tmp.name, "eb"))
        self.patch = mock.patch.object(process, "CHATS", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.st.db.close()
        self.tmp.cleanup()

    def ingest(self, *msgs):
        """msgs: (id, minutes after T0, sender, text[, extra fields])"""
        with open(os.path.join(self.tmp.name, "eb", "messages.jsonl"), "w", encoding="utf-8") as f:
            for i, mins, who, text, *extra in msgs:
                m = {"kind": "message", "id": str(i), "ts": (T0 + dt.timedelta(minutes=mins)).isoformat(timespec="minutes"),
                     "sender": who, "text": text}
                m.update(extra[0] if extra else {})
                f.write(json.dumps(m) + "\n")
        process.ingest(self.st, CFG, force=True)
        return self.st.q("SELECT * FROM entry ORDER BY id")

    def test_mention_after_the_ask(self):
        es = self.ingest((1, 0, "Sam", "pubmat for the GA, deadline Friday 5pm"), (2, 1, "Sam", "@Aldrich"))
        self.assertEqual(len(es), 1)  # the earlier finding is raised, not duplicated
        self.assertEqual(es[0]["kind"], "task")
        self.assertGreaterEqual(es[0]["confidence"], process.SURE)
        self.assertIn("linked by eb#2", es[0]["reasons"])

    def test_mention_before_the_ask(self):
        es = self.ingest((1, 0, "Sam", "@Aldrich"), (2, 2, "Kim", "lol"), (3, 3, "Sam", "pubmat for GA pls, deadline Friday"))
        self.assertEqual([(e["kind"], e["ref"]) for e in es], [("task", "eb#3")])
        m = self.st.one("SELECT reason FROM mention m JOIN item i ON i.id = m.item_id WHERE i.ref = 'eb#3'")
        self.assertIn("in the message before", m["reason"])

    def test_burst_ends_after_a_gap(self):
        es = self.ingest((1, 0, "Sam", "@Aldrich"), (2, 30, "Sam", "pubmat for GA, deadline Friday"))
        self.assertTrue(all(e["kind"] != "task" for e in es))

    def test_reply_that_mentions_you(self):
        es = self.ingest((1, 0, "Kim", "who can do the caption for Friday's post? deadline Thursday"),
                         (2, 40, "Sam", "@Aldrich ikaw na", {"reply_to": "1", "reply_target": "Kim"}))
        task = [e for e in es if e["kind"] == "task"]
        self.assertEqual(len(task), 1)
        self.assertGreaterEqual(task[0]["confidence"], process.SURE)
        self.assertEqual(dt.datetime.fromisoformat(task[0]["due"]).strftime("%a"), "Thu")

    def test_reply_to_you_is_for_you(self):
        es = self.ingest((1, 0, "Aldrich", "I'll make the pubmat"),
                         (2, 5, "Sam", "pa-revise mo, deadline tomorrow 9am", {"reply_to": "1", "reply_target": "Aldrich"}))
        self.assertEqual([e["kind"] for e in es if e["ref"] == "eb#2"], ["task"])

    def test_ok_under_your_own_message_is_nothing(self):
        es = self.ingest((1, 0, "Aldrich", "posting the pubmat Friday 6pm, deadline for edits Thursday"),
                         (2, 3, "Kim", "sige", {"reply_to": "1", "reply_target": "Aldrich"}))
        self.assertFalse([e for e in es if e["ref"] == "eb#2"])

    def test_rescan_after_accepting_a_raised_finding(self):
        es = self.ingest((1, 0, "Sam", "pubmat for the GA, deadline Friday 5pm"), (2, 1, "Sam", "@Aldrich"))
        self.st.update_entry(es[0]["id"], review="accepted")
        process.rescan(self.st, CFG)  # the deadline comes back beside your task; raising it to a task again must not clash
        kept = self.st.one("SELECT * FROM entry WHERE id = ?", (es[0]["id"],))
        self.assertEqual((kept["kind"], kept["review"]), ("task", "accepted"))

    def test_a_rescan_that_fails_changes_nothing(self):
        self.ingest((1, 0, "Sam", "pubmat for the GA, deadline Friday 5pm"), (2, 1, "Sam", "@Aldrich"))
        before = self.st.q("SELECT id, kind FROM entry"), self.st.q("SELECT item_id FROM mention")
        with mock.patch.object(process, "_derive", side_effect=[None, RuntimeError("boom")]):
            with self.assertRaises(RuntimeError):
                process.rescan(self.st, CFG)
        self.assertEqual((self.st.q("SELECT id, kind FROM entry"), self.st.q("SELECT item_id FROM mention")), before)


if __name__ == "__main__":
    unittest.main()
