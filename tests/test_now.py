"""The Now list: each commitment once, one Done for it, and Review only for what's still worth deciding."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import app, process  # noqa: E402
from hubos.store import Store  # noqa: E402

NOW = dt.datetime.now().replace(second=0, microsecond=0)
CFG = {"me": "Aldrich", "profile": {"names": ["Aldrich"], "not_me": []},
       "orgs": {"ecg": {"name": "ECG", "my_title": "DCCO", "above": ["Sam Reyes"], "chats": [{"slug": "eb", "name": "[ECG] EB", "enabled": True}]}}}


def ago(hours):
    return (NOW - dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def date_words(d):
    return d.strftime("%B ") + str(d.day)


class Now(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = Store(os.path.join(self.tmp.name, "hub.db"))
        self.addCleanup(self.st.db.close)
        for patcher in (mock.patch.object(process, "CHATS", self.tmp.name), mock.patch.object(app, "STORE", self.st)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.makedirs(os.path.join(self.tmp.name, "eb"))

    def chat(self, *msgs):
        with open(os.path.join(self.tmp.name, "eb", "messages.jsonl"), "w", encoding="utf-8") as f:
            for i, (hours, who, text) in enumerate(msgs, 1):
                f.write(json.dumps({"kind": "message", "id": str(i), "ts": ago(hours), "sender": who, "text": text}) + "\n")
        process.ingest(self.st, CFG, force=True)

    def dash(self):
        return process.dashboard(self.st, NOW, CFG)

    def ask(self):
        when = NOW + dt.timedelta(days=1)
        self.chat((2, "Sam Reyes", f"@Aldrich can you send the deck? Deadline {date_words(when)} 5pm"))

    def add(self, title, due, confidence=1, kind="deadline", origin="rule", review="unreviewed"):
        self.st.add_entry(kind=kind, title=title, due=due and due.isoformat(timespec="minutes"), due_has_time=1 if due else 0, org="ecg",
                          item_id=None, ref=None, status="open", origin=origin, review=review, confidence=confidence, reasons="")

    def test_a_commitment_and_its_conversation_are_one_row(self):
        self.ask()
        d = self.dash()
        self.assertEqual(len(d["plan"]["now"]), 1)
        row = d["plan"]["now"][0]
        self.assertTrue(row["entry"] and row["card"])
        self.assertEqual(row["who"], "Sam Reyes")

    def test_done_closes_both_and_says_so(self):
        self.ask()
        row = self.dash()["plan"]["now"][0]
        app.resolve(CFG, {"how": "done", "entry": row["entry"], "card": row["card"]})
        d = self.dash()
        self.assertEqual(d["plan"]["now"], [])
        self.assertEqual(d["plan"]["dealt"][0]["how"], "you marked it done")
        e = self.st.one("SELECT status, review FROM entry WHERE id = ?", (row["entry"],))
        self.assertEqual((e["status"], e["review"]), ("done", "accepted"))

    def test_undo_brings_both_back(self):
        self.ask()
        row = self.dash()["plan"]["now"][0]
        app.resolve(CFG, {"how": "done", "entry": row["entry"], "card": row["card"]})
        app.reopen(CFG, {"card": row["card"]})
        self.assertEqual(len(self.dash()["plan"]["now"]), 1)

    def test_not_for_me_records_a_rejection(self):
        self.ask()
        row = self.dash()["plan"]["now"][0]
        app.resolve(CFG, {"how": "drop", "entry": row["entry"], "card": row["card"]})
        e = self.st.one("SELECT status, review FROM entry WHERE id = ?", (row["entry"],))
        self.assertEqual((e["status"], e["review"]), ("dismissed", "rejected"))

    def test_a_new_ping_after_done_brings_it_back(self):
        ask = f"@Aldrich can you send the deck? Deadline {date_words(NOW + dt.timedelta(days=1))} 5pm"
        self.chat((2, "Sam Reyes", ask))
        row = self.dash()["plan"]["now"][0]
        app.resolve(CFG, {"card": row["card"]})
        self.assertEqual(self.dash()["plan"]["now"], [])
        self.chat((2, "Sam Reyes", ask), (1, "Sam Reyes", "@Aldrich did you see this?"))
        self.assertEqual(len([c for c in self.dash()["cards"] if c["state"] == "needs"]), 1)

    def test_overdue_comes_before_something_later(self):
        self.add("later", NOW + dt.timedelta(days=1), kind="task", origin="capture", review="accepted")
        self.add("overdue", NOW - dt.timedelta(hours=1), kind="task", origin="capture", review="accepted")
        self.assertEqual(self.dash()["plan"]["now"][0]["title"], "overdue")

    def test_review_holds_the_unsure_and_keeps_the_gone_apart(self):
        self.add("unsure ahead", NOW + dt.timedelta(days=3), 0.7)
        self.add("unsure gone", NOW - dt.timedelta(days=3), 0.7)
        self.add("sure ahead", NOW + dt.timedelta(days=3), 0.9)
        self.add("sure gone", NOW - dt.timedelta(days=3), 0.9)
        d = self.dash()
        self.assertEqual([e["title"] for e in d["review"]], ["unsure ahead"])
        self.assertEqual(d["review_count"], 1)
        self.assertCountEqual([e["title"] for e in d["review_expired"]], ["unsure gone", "sure gone"])
        self.assertEqual(d["review_expired_count"], 2)
        self.assertEqual([n["title"] for n in d["plan"]["next"]], ["sure ahead"], "what's sure skips Review; what's dead skips the list")

    def test_the_brief_counts_each_org(self):
        self.ask()
        org = self.dash()["brief"]["orgs"][0]
        self.assertEqual((org["org"], org["messages"], org["pings"], org["waiting"]), ("ecg", 1, 1, 1))

    def test_undated_notes_stay_out_of_now(self):
        self.add("an idea", None, kind="capture", origin="capture", review="accepted")
        d = self.dash()
        self.assertEqual(d["plan"]["now"], [])
        self.assertEqual(d["notes"][0]["title"], "an idea")


if __name__ == "__main__":
    unittest.main()
