"""Bot + scheduler logic with a fake Telegram (no network)."""

import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import bot as botmod  # noqa: E402
from hubos import process, settings  # noqa: E402
from hubos.store import Store  # noqa: E402

CFG = {
    "me": "Aldrich",
    "profile": {"names": ["Aldrich"]},
    "orgs": {"ecg": {"name": "ECG", "chats": []}},
    "settings": {"ping_cap": 3},
}
NOW = dt.datetime(2026, 10, 27, 7, 5)  # Tuesday morning


class FakeBot(botmod.Bot):
    def __init__(self, st, secrets):
        super().__init__(st, lambda: CFG)
        self.out, self.sec = [], secrets

    def _secrets(self):
        return self.sec

    def send(self, text, chat_id=None):
        self.out.append(text)
        return True


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "hub.db"))
        self.bot = FakeBot(self.st, {"telegram_token": "x", "telegram_chat_id": 42, "pair_code": "123456"})

    def tearDown(self):
        self.st.db.close()
        self.tmp.cleanup()

    def add(self, text, when):
        with mock.patch.object(process.dt, "datetime", wraps=dt.datetime) as m:
            m.now.return_value = when
            return process.capture(self.st, text, "app", CFG)

    def test_morning_brief_once(self):
        self.add("GA pubmat due today 6pm #ecg", NOW)
        sent = self.bot.tick(NOW)
        self.assertIn("brief:2026-10-27:morning", sent)
        self.assertIn("ECG · note · today 6:00 PM", self.bot.out[0])
        self.assertEqual(self.bot.tick(NOW + dt.timedelta(minutes=1)), [])  # not twice

    def test_quiet_hours_and_stale_briefs(self):
        self.assertEqual(self.bot.tick(dt.datetime(2026, 10, 27, 23, 30)), [])  # quiet
        self.assertFalse([k for k in self.bot.tick(dt.datetime(2026, 10, 27, 11, 0)) if "morning" in k])  # > 3 h late

    def test_alerts_capped_and_deduped(self):
        for i in range(5):
            self.add(f"thing {i} today 8:00am", NOW)
        sent = [k for k in self.bot.tick(NOW) if k.startswith("alert:")]
        self.assertEqual(len(sent), 3)  # ping_cap
        self.assertEqual([k for k in self.bot.tick(NOW) if k.startswith("alert:")], [])

    def test_redaction(self):
        e = {"id": 1, "kind": "task", "org": "ecg", "origin": "rule", "due": None, "due_has_time": 0,
             "title": "@Patricia Reyes send to sam@example.edu.ph 09171234567 https://x.co/a and @Aldrich"}
        out = botmod.line(e, NOW, "short", ["Aldrich"])
        for bad in ("Patricia", "dlsu.edu", "0917", "https"):
            self.assertNotIn(bad, out)
        self.assertIn("@Aldrich", out)
        self.assertNotIn("send", botmod.line(e, NOW, "minimal", ["Aldrich"]))

    def test_pairing_and_commands(self):
        b = FakeBot(self.st, {"telegram_token": "x", "pair_code": "654321"})
        with mock.patch.object(settings, "save_secrets"):
            b.handle(7, "/start 111111")
            self.assertNotIn("telegram_chat_id", b.sec)
            b.handle(7, "/start 654321")
            self.assertEqual(b.sec["telegram_chat_id"], 7)
        b.handle(8, "hello")  # stranger: ignored
        self.assertTrue(all("Saved" not in t for t in b.out))
        b.handle(7, "submit plates bukas 5pm #ecg")
        self.assertIn("Saved #", b.out[-1])
        eid = int(b.out[-1].split("#")[1].split(":")[0])
        b.handle(7, f"/done {eid}")
        self.assertEqual(self.st.one("SELECT status FROM entry WHERE id = ?", (eid,))["status"], "done")
        b.handle(7, "/win ran the EB alignment")
        self.assertEqual(self.st.one("SELECT COUNT(*) AS n FROM entry WHERE kind = 'win'")["n"], 1)

    def test_quiet_window(self):
        q = {"start": "23:00", "end": "07:00"}
        self.assertTrue(botmod.in_quiet(dt.datetime(2026, 1, 1, 2, 0), q))
        self.assertFalse(botmod.in_quiet(dt.datetime(2026, 1, 1, 7, 0), q))


if __name__ == "__main__":
    unittest.main()
