"""messenger/watch.js on a made-up chat list: sees changes, opens nothing, and never returns row text."""

import json
import os
import sys
import tempfile
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "messenger"))
with open(os.path.join(ROOT, "messenger", "watch.js"), encoding="utf-8") as f:
    SCRIPT = f.read()
PAGE = """<div id=list>
<a id=one href="/messages/t/111"><span>Kim's chat</span><br><span id=p1>Kim: first secret</span></a>
<a id=two href="/messages/e2ee/t/222"><span>Other</span><br><span id=p2 style="font-weight:700">Gio: hello</span></a>
</div><div role=log><a href="/messages/t/999"><span>a link inside a chat</span></a></div>"""


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class WatchList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:  # pragma: no cover
            cls.pw.stop()
            raise unittest.SkipTest(f"Chromium isn't available: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(PAGE)
        self.page.evaluate(SCRIPT)

    def tearDown(self):
        self.page.close()

    def test_lists_rows_outside_the_open_chat(self):
        rows = self.page.evaluate("window.__hsw.drain().rows")
        self.assertEqual({r["id"] for r in rows}, {"111", "222"})
        self.assertEqual(next(r for r in rows if r["id"] == "222")["weight"], 700)

    def test_a_new_preview_is_a_change(self):
        self.page.evaluate("window.__hsw.drain()")
        self.page.evaluate("document.getElementById('p1').textContent = 'Kim: second secret'")
        self.page.wait_for_timeout(50)
        d = self.page.evaluate("window.__hsw.drain()")
        self.assertEqual([c["id"] for c in d["changes"]], ["111"])
        self.assertGreater(d["mutations"], 0)
        self.assertEqual(self.page.evaluate("window.__hsw.drain().changes"), [], "a change is reported once")

    def test_time_labels_dont_change_the_signature(self):
        for first, second in (("Kim: hi · 26m", "Kim: hi · 27m"), ("Kim: hi\n·\n59m", "Kim: hi\n·\n1h"),
                              ("Kim: hi · Just now", "Kim: hi · 3m"), ("Kim: hi · 1h", "Kim: hi · Mon"),
                              ("Kim: hi · 6d", "Kim: hi · Oct 12"), ("Unread message: Kim: hi", "Kim: hi")):
            self.page.evaluate("t => { document.getElementById('p1').innerText = t }", first)
            self.page.wait_for_timeout(20)
            self.page.evaluate("window.__hsw.drain()")
            self.page.evaluate("t => { document.getElementById('p1').innerText = t }", second)
            self.page.wait_for_timeout(20)
            self.assertEqual(self.page.evaluate("window.__hsw.drain().changes"), [], f"{first} -> {second}")

    def test_a_new_message_still_changes_it_next_to_a_time(self):
        self.page.evaluate("t => { document.getElementById('p1').innerText = t }", "Kim: hi · 26m")
        self.page.wait_for_timeout(20)
        self.page.evaluate("window.__hsw.drain()")
        self.page.evaluate("t => { document.getElementById('p1').innerText = t }", "Kim: see you at 3 · 27m")
        self.page.wait_for_timeout(20)
        self.assertEqual([c["id"] for c in self.page.evaluate("window.__hsw.drain().changes")], ["111"])

    def test_rows_come_with_their_place_in_the_list(self):
        rows = self.page.evaluate("window.__hsw.drain().rows")
        self.assertEqual([(r["id"], r["rank"]) for r in rows], [("111", 0), ("222", 1)])

    def test_a_change_outside_the_rows_is_ignored(self):
        self.page.evaluate("window.__hsw.drain()")
        self.page.evaluate("document.querySelector('[role=log] span').textContent = 'new message'")
        self.page.wait_for_timeout(50)
        d = self.page.evaluate("window.__hsw.drain()")
        self.assertEqual((d["changes"], d["mutations"]), ([], 0))

    def test_no_row_text_leaves_the_page(self):
        self.page.evaluate("document.getElementById('p1').textContent = 'Kim: second secret'")
        self.page.wait_for_timeout(50)
        out = json.dumps(self.page.evaluate("window.__hsw.drain()"))
        for word in ("secret", "Kim", "Gio", "hello"):
            self.assertNotIn(word, out)

    def test_nothing_gets_clicked(self):
        self.page.evaluate("window.__clicks = 0; document.addEventListener('click', () => window.__clicks++, true)")
        self.page.evaluate("window.__hsw.drain()")
        self.assertEqual(self.page.evaluate("window.__clicks"), 0)
        self.assertEqual(self.page.url, "about:blank")


class Summary(unittest.TestCase):
    def test_summary_counts_changes_by_chat(self):
        import probe

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.jsonl")
            rows = [{"kind": "start"},
                    {"kind": "tick", "changes": [{"id": "1"}, {"id": "1"}], "frames": {"h|in": 2}},
                    {"kind": "tick", "changes": [{"id": "2"}], "frames": {"h|in": 3}},
                    {"kind": "stop", "why": "logged out"}]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r) for r in rows))
            s = probe.summary(path)
        self.assertEqual((s["ticks"], s["row_changes"], s["chats_changed"]), (2, 3, 2))
        self.assertEqual(s["frames"], {"h|in": 5})
        self.assertEqual(s["busiest"][0], ("1", 2))
        self.assertEqual(s["stopped"], ["logged out"])


if __name__ == "__main__":
    unittest.main()
