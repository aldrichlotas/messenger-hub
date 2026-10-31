"""The chat list refresh on a made-up Messenger list: it starts short, grows, and mounts only a few rows at a time."""

import datetime as dt
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_ui  # noqa: E402
import hub  # noqa: E402

with open(os.path.join(ROOT, "messenger", "discover.js"), encoding="utf-8") as f:
    SCRIPT = f.read()

# Rows are 60px in a 600px list. Only the rows on screen (and one each side) exist in the page, like Messenger's.
PAGE = """<div id=list style="height:600px;overflow-y:auto;position:relative"><div id=inner></div></div>
<script>
const params = new URLSearchParams(location.hash.slice(1));
const TOTAL = +params.get('total'), START = +params.get('start'), BATCH = +params.get('batch'), OLD = +params.get('old');
let loaded = START;
const list = document.getElementById('list'), inner = document.getElementById('inner');
function paint() {
  inner.style.height = loaded * 60 + 'px';
  inner.textContent = '';
  const first = Math.max(0, Math.floor(list.scrollTop / 60) - 1);
  for (let i = first; i < Math.min(loaded, first + 12); i++) {
    const a = document.createElement('a');
    a.href = '/messages/t/' + i;
    a.innerText = 'Chat ' + i + '\\nPreview' + (OLD ? ' · ' + (i >= OLD ? '2y' : '3d') : '');
    a.style.cssText = 'position:absolute;display:block;height:60px;top:' + i * 60 + 'px';
    inner.appendChild(a);
  }
}
function more() {
  if (loaded < TOTAL) { loaded = Math.min(TOTAL, loaded + BATCH); paint(); }
}
list.addEventListener('scroll', () => {
  paint();
  if (list.scrollTop + list.clientHeight > list.scrollHeight - 200) setTimeout(more, 20);
});
setTimeout(more, 150);  // a moment after loading, the short first list grows
paint();
</script>"""


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class WalkingTheList(unittest.TestCase):
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

    def read(self, total, start=7, batch=25, old=0, find_all=False):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=PAGE))
        page.goto(f"http://hub.invalid/messages/new/#total={total}&start={start}&batch={batch}&old={old}")
        page.evaluate(SCRIPT)
        page.evaluate("window.__hsd.collect()")
        return hub.read_chat_list(page, pause=0.03, find_all=find_all)

    def test_a_list_that_starts_too_short_to_scroll_gets_read_in_full(self):
        self.assertEqual(len(self.read(120)), 120)

    def test_no_chat_is_skipped_between_screens(self):
        self.assertEqual(sorted(int(c["id"]) for c in self.read(300)), list(range(300)))

    def test_a_short_list_that_never_scrolls_still_finishes(self):
        self.assertEqual(len(self.read(5, start=5)), 5)

    def test_stops_soon_after_chats_go_quiet(self):
        got = self.read(300, old=40)
        self.assertLess(len(got), 70)
        self.assertEqual(sorted(int(c["id"]) for c in got)[:40], list(range(40)), "every active chat is in")

    def test_find_all_goes_past_quiet_chats_to_the_end(self):
        self.assertEqual(len(self.read(300, old=40, find_all=True)), 300)

    def test_rows_it_cant_read_never_end_it_early(self):
        self.assertEqual(len(self.read(120)), 120)

    def test_lines_are_kept_for_naming(self):
        self.assertEqual(self.read(10, start=10)[0]["lines"], ["Chat 0", "Preview"])


class Ages(unittest.TestCase):
    TODAY = dt.date(2026, 10, 30)

    def age(self, *lines):
        return hub.age_days(list(lines), self.TODAY)

    def test_the_labels_messenger_uses(self):
        for label, days in (("3d", 3), ("2w", 14), ("52w", 364), ("53w", 371), ("1y", 365), ("2y", 730), ("5h", 0), ("6 mo", 180),
                            ("2 years ago", 730), ("3 weeks", 21)):
            self.assertEqual(self.age("Chat", "Kim: hello", label), days, label)

    def test_a_label_on_the_preview_line(self):
        self.assertEqual(self.age("Chat", "Kim: hello · 2y"), 730)

    def test_a_label_on_its_own_after_a_dot_line(self):
        self.assertEqual(self.age("Chat", "Kim: hello", "·", "2y"), 730)

    def test_a_date_with_a_year_counts_from_that_years_end(self):
        self.assertEqual(self.age("Chat", "Oct 12, 2024"), 668)

    def test_a_date_without_a_year_is_recent(self):
        self.assertIsNone(self.age("Chat", "Oct 12"))

    def test_minutes_arent_months(self):
        self.assertIsNone(self.age("Chat", "Kim: hi · 26m"))

    def test_a_preview_isnt_a_label(self):
        self.assertIsNone(self.age("Chat", "see you in 2y okay then"))
        self.assertIsNone(self.age("Chat", "Kim: done in 3d"))

    def test_no_label(self):
        self.assertIsNone(self.age("Chat", "Active now"))
        self.assertIsNone(self.age())

    def test_quiet_rows_count_from_the_end(self):
        rows = [{"lines": ["a", x]} for x in ("3d", "2y", "1y", "Active now", "2y")]
        self.assertEqual(hub.quiet_rows(rows, self.TODAY), 3)

    def test_one_old_row_among_active_ones_doesnt_stop_it(self):
        rows = [{"lines": ["a", x]} for x in ("2y", "3d", "4d")]
        self.assertEqual(hub.quiet_rows(rows, self.TODAY), 0)


class FindingAll(unittest.TestCase):
    def test_off_by_default_on_the_command_line_too(self):
        with mock.patch.object(config_ui.subprocess, "run") as run:
            config_ui.run_discover()
            self.assertNotIn("--all-chats", run.call_args[0][0])
            config_ui.run_discover(True)
            self.assertIn("--all-chats", run.call_args[0][0])


class FakeWalk:
    """collect() answers from a script; step() says whether we're at the bottom."""

    def __init__(self, counts, end=True):
        self.counts, self.end, self.calls, self.list = iter(counts), end, 0, []

    def evaluate(self, js):
        if "collect" in js:
            self.calls += 1
            return next(self.counts, 7)
        if "step" in js:
            return {"ok": True, "end": self.end}
        return self.list


class Stopping(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(hub.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_stops_at_the_bottom_once_nothing_new_loads(self):
        page = FakeWalk([3, 7])
        hub.read_chat_list(page)
        self.assertEqual(page.calls, 2 + hub.LIST_END_ROUNDS)

    def test_a_slow_load_at_the_bottom_doesnt_end_it_early(self):
        counts = [7] * (hub.LIST_END_ROUNDS - 1) + [30, 31]
        page = FakeWalk(counts)
        hub.read_chat_list(page)
        self.assertEqual(page.calls, len(counts) + hub.LIST_END_ROUNDS)

    def test_gives_up_on_a_list_that_never_moves(self):
        page = FakeWalk([], end=False)
        hub.read_chat_list(page)
        self.assertEqual(page.calls, hub.LIST_STUCK_ROUNDS + 1)


class StoppingPartWay(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.stop = os.path.join(d.name, "STOP")
        for target, value in ((hub, "STOP_FILE"), (hub.time, "sleep")):
            p = mock.patch.object(target, value, self.stop if value == "STOP_FILE" else (lambda s: None))
            p.start()
            self.addCleanup(p.stop)

    def press_stop_after(self, rounds):
        page = FakeWalk(list(range(1, 1000)), end=False)
        collect = page.evaluate

        def evaluate(js):
            if "collect" in js and page.calls == rounds:
                open(self.stop, "w").close()
            return collect(js)

        page.evaluate = evaluate
        return page

    def test_stops_at_the_next_screen(self):
        page = self.press_stop_after(3)
        hub.read_chat_list(page)
        self.assertEqual(page.calls, 4)

    def test_what_was_read_is_returned(self):
        page = self.press_stop_after(3)
        page.list = [{"id": "1"}]
        self.assertEqual(hub.read_chat_list(page), [{"id": "1"}])

    def test_the_stop_is_used_up(self):
        hub.read_chat_list(self.press_stop_after(1))
        self.assertFalse(os.path.exists(self.stop))

    def test_it_also_stops_with_find_all(self):
        page = self.press_stop_after(2)
        hub.read_chat_list(page, find_all=True)
        self.assertEqual(page.calls, 3)


class ClosingTheWindow(unittest.TestCase):
    def test_what_was_read_before_it_closed_is_kept(self):
        page = FakeWalk(list(range(1, 100)), end=False)
        page.list = [{"id": "1"}, {"id": "2"}]
        step = page.evaluate

        def evaluate(js):
            if "collect" in js and page.calls == 3:
                raise RuntimeError("Target page, context or browser has been closed")
            return step(js)

        page.evaluate = evaluate
        with mock.patch.object(hub.time, "sleep", lambda s: None):
            self.assertEqual(hub.read_chat_list(page), [{"id": "1"}, {"id": "2"}])

    def test_closed_before_anything_was_read_keeps_nothing(self):
        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("closed")
        self.assertEqual(hub.read_chat_list(page), [])


class StopRequest(unittest.TestCase):
    def post(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), config_ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/discover-stop", data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.stop = os.path.join(d.name, "STOP")
        p = mock.patch.object(hub, "STOP_FILE", self.stop)
        p.start()
        self.addCleanup(p.stop)

    def test_stop_during_a_refresh_leaves_the_stop_file(self):
        with config_ui._discovering:
            self.post()
        self.assertTrue(os.path.exists(self.stop))

    def test_stop_with_no_refresh_does_nothing(self):
        self.post()
        self.assertFalse(os.path.exists(self.stop))


class Merging(unittest.TestCase):
    def test_a_short_refresh_cant_shrink_the_list(self):
        old = [{"id": str(n)} for n in range(195)]
        got = hub.merge_chat_lists([{"id": "1"}, {"id": "new"}], old)
        self.assertEqual(len(got), 196)

    def test_what_this_refresh_found_comes_first_and_wins(self):
        got = hub.merge_chat_lists([{"id": "1", "lines": ["new"]}], [{"id": "2"}, {"id": "1", "lines": ["old"]}])
        self.assertEqual(got, [{"id": "1", "lines": ["new"]}, {"id": "2"}])

    def test_a_first_refresh_has_nothing_to_merge(self):
        self.assertEqual(hub.merge_chat_lists([{"id": "1"}], []), [{"id": "1"}])


if __name__ == "__main__":
    unittest.main()
