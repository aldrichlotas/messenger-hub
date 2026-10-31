"""The demo must never reach real data: it's what computer use and other people get to look at."""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = r"""
import json, os, sys, webbrowser
sys.path.insert(0, os.path.join(sys.argv[1], "demo"))
import run_demo
hub, app, process = run_demo.isolate("normal")
from hubos import live, settings, store, threads
webbrowser.open("https://example.com")
print(json.dumps({
    "demo": run_demo.DEMO,
    "paths": [store.DB, process.CHATS, hub.CONFIG, hub.CHATS, hub.ORGS, hub.CHAT_LIST, hub.LIST_STATE, hub.STOP_FILE, hub.DATA,
              settings.SECRETS, settings.ENV_FILE, live.DIR, app.STARTUP_FILE, app.DESKTOP_FILE],
    "secrets": settings.load_secrets(),
    "telegram_env": [k for k in os.environ if k.startswith("TELEGRAM_")],
    "sync": app.run_sync(), "summary": threads.summarise(None, None, None),
}))
"""


class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "TELEGRAM_BOT_TOKEN": "real-token", "HUB_DB": os.path.join(ROOT, "data", "hub.db")}
        out = subprocess.run([sys.executable, "-c", PROBE, ROOT], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
        cls.seen = json.loads(out.stdout.strip().splitlines()[-1])

    def test_every_path_stays_in_the_demo_folder(self):
        base = os.path.realpath(self.seen["demo"]) + os.sep
        for path in self.seen["paths"]:
            self.assertTrue(os.path.realpath(path).startswith(base) or os.path.realpath(path) == base[:-1], path)

    def test_no_telegram_credentials_get_through(self):
        self.assertEqual(self.seen["telegram_env"], [])
        self.assertFalse(self.seen["secrets"].get("telegram_token"))

    def test_sync_and_summaries_do_nothing(self):
        self.assertIsNone(self.seen["sync"])
        self.assertIsNone(self.seen["summary"])


if __name__ == "__main__":
    unittest.main()
