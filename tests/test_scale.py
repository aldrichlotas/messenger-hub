"""Budget check: the app's data stays fast with every org synced (made-up messages, no network, no tokens).

26 chats x 2000 messages is about what ECG + IMES add on top of passion projects. The app builds the cards on
every request and refreshes each minute, so cards() and dashboard() have to stay well under a second.
"""

import datetime as dt
import json
import os
import random
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import process, threads  # noqa: E402
from hubos.store import Store  # noqa: E402

CHATS, MSGS = 26, 2000
BUDGET_MS = {"cards": 1000, "dashboard": 1500, "ingest": 60000}  # ingest: the whole first sync
YARDSTICK, YARDSTICK_MS = 1000000, 50  # a fixed loop, and how long it takes on a fast core
PEOPLE = ["Sam", "Kim", "Bea", "Cy", "Dee", "Enzo", "Gab", "Isa", "Jo", "Lia", "Mika", "Nico", "Pia", "Rey", "Tin"]
WORDS = ("pubmat caption deadline bukas mamaya meeting GA reels layout ok sige noted salamat pls check send "
         "upload sino ano kailan pano lol haha tapos na ready na approve edit poster slides").split()


def org_cfg():
    chats = [{"slug": f"c{i}", "name": f"[ECG] Chat {i}", "enabled": True} for i in range(CHATS)]
    return {"me": "Aldrich", "profile": {"names": ["Aldrich"]},
            "orgs": {"ecg": {"name": "ECG", "my_title": "DCCO", "aliases": ["Deputy CCO"], "committees": ["Creatives"],
                             "above": ["Sam"], "chats": chats}}}


class Scale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        rnd = random.Random(7)
        start = dt.datetime.now() - dt.timedelta(days=30)
        for c in range(CHATS):
            os.makedirs(os.path.join(cls.tmp.name, f"c{c}"))
            t = start
            with open(os.path.join(cls.tmp.name, f"c{c}", "messages.jsonl"), "w", encoding="utf-8") as f:
                for i in range(MSGS):
                    t += dt.timedelta(minutes=rnd.choice([1, 1, 2, 3, 5, 20, 90]))
                    who = "Aldrich" if rnd.random() < 0.1 else rnd.choice(PEOPLE)
                    text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 9)))
                    r = rnd.random()
                    if r < 0.04:
                        text = "@Aldrich " + text + "?"
                    elif r < 0.06:
                        text = "Creatives " + text
                    elif r < 0.07:
                        text = "DCCO pa-check " + text
                    f.write(json.dumps({"kind": "message", "id": str(i + 1), "ts": t.isoformat(timespec="minutes"),
                                        "sender": who, "text": text}) + "\n")
        cls.patch = mock.patch.object(process, "CHATS", cls.tmp.name)
        cls.patch.start()
        cls.cfg = org_cfg()
        cls.st = Store(os.path.join(cls.tmp.name, "hub.db"))
        t = time.perf_counter()
        process.ingest(cls.st, cls.cfg, force=True)
        cls.ingest_s = time.perf_counter() - t

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.st.db.close()
        cls.tmp.cleanup()

    def timed(self, fn):
        """The fastest of five runs, in fast-core milliseconds. Windows may put a run on an efficiency core,
        which is about 1.5 times slower for this work: a fixed loop timed just before each run shows which it got."""
        fn()  # warm up
        best = float("inf")
        for _ in range(5):
            t = time.perf_counter()
            sum(i * i for i in range(YARDSTICK))
            core = max(1, (time.perf_counter() - t) * 1000 / YARDSTICK_MS)
            t = time.perf_counter()
            out = fn()
            best = min(best, (time.perf_counter() - t) / core)
        return out, best * 1000

    def test_first_sync_ingest_within_budget(self):
        self.assertLess(self.ingest_s * 1000, BUDGET_MS["ingest"], "one commit per chat, not per message")
        self.assertEqual(self.st.one("SELECT COUNT(*) AS n FROM item")["n"], CHATS * MSGS)

    def test_batched_writes_are_saved(self):
        path = os.path.join(self.tmp.name, "batch.db")
        st = Store(path)
        with st.batch():
            st.set_meta("a", "1")
            st.set_meta("b", "2")
        st.db.close()
        again = Store(path)
        self.assertEqual((again.get_meta("a"), again.get_meta("b")), ("1", "2"))
        again.db.close()

    def test_cards_and_dashboard_within_budget(self):
        chans, prof = process.channels(self.cfg), process.profile(self.cfg)
        cs, ms = self.timed(lambda: threads.cards(self.st, chans, prof))
        self.assertGreater(len(cs), 100, "the made-up chats should produce plenty of cards")
        self.assertLess(ms, BUDGET_MS["cards"], f"cards() took {ms:.0f} ms for {len(cs)} cards")
        d, ms = self.timed(lambda: process.dashboard(self.st, cfg=self.cfg))
        self.assertLess(ms, BUDGET_MS["dashboard"], f"dashboard() took {ms:.0f} ms")
        self.assertLess(len(json.dumps(d)) / 1e6, 8, "the app's JSON stays a reasonable size")
        print(f"\n  scale: {CHATS * MSGS} msgs ingested in {self.ingest_s:.1f}s, {len(cs)} cards, "
              f"dashboard {ms:.0f} ms, {len(json.dumps(d)) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
