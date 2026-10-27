"""Conversation split and mention cards (no network, no tokens)."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import process, threads  # noqa: E402
from hubos.store import Store  # noqa: E402

CFG = {
    "me": "Aldrich",
    "profile": {"names": ["Aldrich"]},
    "orgs": {"ecg": {"name": "ECG", "chats": [{"slug": "eb", "name": "[ECG] EB", "enabled": True}],
                     "committees": ["Creatives"], "above": ["Sam"]}},
}
T0 = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=1)


class ThreadTests(unittest.TestCase):
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

    def cards(self, *msgs):
        """msgs: (id, minutes after T0, sender, text[, extra fields])"""
        with open(os.path.join(self.tmp.name, "eb", "messages.jsonl"), "w", encoding="utf-8") as f:
            for i, mins, who, text, *extra in msgs:
                m = {"kind": "message", "id": str(i), "ts": (T0 + dt.timedelta(minutes=mins)).isoformat(timespec="minutes"),
                     "sender": who, "text": text}
                m.update(extra[0] if extra else {})
                f.write(json.dumps(m) + "\n")
        process.ingest(self.st, CFG, force=True)
        return threads.cards(self.st, process.channels(CFG), process.profile(CFG))

    def test_one_card_per_conversation(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich"), (2, 1, "Patrick", "turn on your mic"), (3, 2, "Patrick", "@Aldrich?"),
                        (4, 120, "Kim", "@Aldrich what roles do we have"))  # two hours later: a new conversation
        self.assertEqual(len(cs), 2)
        first = [c for c in cs if c["asker"] == "Patrick"][0]
        self.assertEqual(first["pings"], 2)
        self.assertEqual(first["label"], "question")  # "@Aldrich" alone is read with the rest of the burst
        self.assertIsNone(first["handled"])
        self.assertTrue(any(ln.get("text") == "turn on your mic" for ln in first["excerpt"]))

    def test_handled_by_your_reply(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich can you check the build"), (2, 3, "Aldrich", "on it"))
        self.assertEqual(cs[0]["handled"]["how"], "you replied")
        self.assertFalse(cs[0]["unread"])
        self.assertEqual([ln["role"] for ln in cs[0]["excerpt"]][-1], "reply")

    def test_late_message_is_not_a_reply(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich can you check the build"), (2, 20, "Kim", "same q"),
                        (3, 40, "Kim", "anyone?"), (4, 70, "Kim", "hello"), (5, 95, "Aldrich", "brb dinner"))
        self.assertIsNone(cs[0]["handled"])  # you spoke 95 minutes later, about something else

    def test_reply_joins_its_conversation(self):
        cs = self.cards((1, 0, "Sam", "Creatives: who can make the GA pubmat?"), (2, 200, "Kim", "random"),
                        (3, 300, "Sam", "Aldrich ikaw na", {"reply_to": "1", "reply_target": "Sam"}))
        card = [c for c in cs if c["asker"] == "Sam"][0]
        self.assertTrue(any("GA pubmat" in (ln.get("text") or "") for ln in card["excerpt"]))
        self.assertTrue(card["above"])

    def test_banter_reply_to_you_needs_nothing(self):
        cs = self.cards((1, 0, "Aldrich", "I gave Gemini access to my laptop"),
                        (2, 400, "Kim", "AI beef", {"reply_to": "1", "reply_target": "Aldrich"}))
        self.assertEqual(cs[0]["handled"]["how"], "nothing asked")

    def test_answered_by_someone_else(self):
        cs = self.cards((1, 0, "Sam", "Creatives, sino may copy ng logo?"),
                        (2, 4, "Kim", "ako, sending", {"reply_to": "1", "reply_target": "Sam"}))
        self.assertEqual(cs[0]["handled"]["how"], "answered by Kim")

    def test_read_mark_and_new_ping(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich are you free"))
        threads.mark_read(self.st, cs[0]["key"], cs[0]["last_ping"], cs[0]["ping_items"])
        self.assertFalse(threads.cards(self.st, process.channels(CFG), process.profile(CFG))[0]["unread"])
        cs = self.cards((1, 0, "Patrick", "@Aldrich are you free"), (2, 5, "Patrick", "@Aldrich??"))
        self.assertTrue(cs[0]["unread"])  # a new ping after the read mark

    def test_duplicate_rows_shown_once(self):
        cs = self.cards((1, 0, "Gab", "using @Aldrich laptop"), (2, 0, "Gab", "using @Aldrich laptop"), (3, 0, "Gab", ""))
        self.assertEqual(sum(1 for ln in cs[0]["excerpt"] if "gap" not in ln), 1)


if __name__ == "__main__":
    unittest.main()
