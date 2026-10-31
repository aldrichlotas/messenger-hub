"""hubos/live.py on made-up data, with a fake browser: no Facebook, no real hub data."""

import datetime as dt
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import hub  # noqa: E402,F401  (puts messenger/ on the path)
import scrape  # noqa: E402
from hubos import bot as botmod  # noqa: E402
from hubos import live  # noqa: E402


def row(i, sig="a", weight=500, rank=0):
    return {"id": i, "sig": sig, "weight": weight, "rank": rank, "lines": 2}


class Detecting(unittest.TestCase):
    def setUp(self):
        self.d = live.Detector()
        self.d.read([row("1", rank=0), row("2", rank=1), row("3", rank=2), row("4", rank=3), row("5", rank=4)])

    def test_first_read_is_a_baseline_except_unread_chats(self):
        d = live.Detector()
        self.assertEqual(d.read([row("1"), row("2", weight=700, rank=1)]), ["2"])

    def test_a_new_preview_is_a_change(self):
        self.assertEqual(self.d.read([row("1", "b")]), ["1"])

    def test_no_change_is_nothing(self):
        self.assertEqual(self.d.read([row("1"), row("2", rank=1)]), [])

    def test_turning_plain_after_reading_on_the_phone_is_nothing(self):
        d = live.Detector()
        d.read([row("1", weight=700)])
        self.assertEqual(d.read([row("1", weight=500)]), [])

    def test_turning_bold_with_the_same_text_is_a_change(self):
        self.assertEqual(self.d.read([row("1", weight=700)]), ["1"])

    def test_a_new_chat_landing_on_top_is_a_change(self):
        self.assertEqual(self.d.read([row("9", rank=0)]), ["9"])

    def test_a_chat_scrolling_into_view_lower_down_is_nothing(self):
        self.assertEqual(self.d.read([row("9", rank=8)]), [])

    def test_a_row_coming_back_unchanged_after_a_reload_is_nothing(self):
        self.d.read([row("1")])
        self.assertEqual(self.d.read([row("2", rank=1)]), [])
        self.assertEqual(self.d.read([row("1", rank=4)]), [])

    def test_a_clump_keeps_only_the_top_of_the_list(self):
        rows = [row(str(n), "b", rank=n - 1) for n in range(1, 6)]
        self.assertEqual(self.d.read(rows), ["1", "2", "3"])
        self.assertEqual(self.d.noise, 2)


class Pacing(unittest.TestCase):
    def setUp(self):
        self.p = live.Pacer(random.Random(1))

    def test_nothing_waiting_is_nothing(self):
        self.assertIsNone(self.p.next(100))

    def test_oldest_first_with_a_gap_between_opens(self):
        self.p.add("a", "list")
        self.p.add("b", "list")
        self.assertEqual(self.p.next(100), ("a", "list"))
        self.p.opened("a", 100)
        self.assertIsNone(self.p.next(101))
        self.assertEqual(self.p.next(100 + live.OPEN_GAP[1]), ("b", "list"))

    def test_a_busy_chat_waits_its_cooldown_but_stays_queued(self):
        self.p.opened("a", 100)
        self.p.add("a", "list")
        self.assertIsNone(self.p.next(100 + live.OPEN_GAP[1] + 1))
        self.assertEqual(self.p.next(100 + live.COOLDOWN[1]), ("a", "list"))

    def test_the_slowest_wait_still_fits_the_two_minute_target(self):
        self.assertLess(live.TICK + live.COOLDOWN[1] + live.PATIENCE / 3, 120)

    def test_catchup_takes_the_chat_looked_at_longest_ago(self):
        got = self.p.catchup(100, ["a", "b", "c"], {"a": "2026-10-30T10:00", "b": "2026-10-29T10:00", "c": "2026-10-30T11:00"})
        self.assertEqual(got, "b")

    def test_a_chat_never_looked_at_goes_first(self):
        self.assertEqual(self.p.catchup(100, ["a", "b"], {"a": "2026-10-30T10:00"}), "b")

    def test_catchup_is_spread_over_the_whole_pass(self):
        chats = [str(n) for n in range(27)]
        self.assertIsNotNone(self.p.catchup(100, chats, {}))
        self.assertIsNone(self.p.catchup(101, chats, {}))
        turn = live.CATCHUP_HOURS * 3600 / 27
        self.assertIsNotNone(self.p.catchup(100 + turn * 1.3, chats, {}))

    def test_a_whole_pass_costs_less_than_the_daily_limit(self):
        self.assertLess(27 * 24 / live.CATCHUP_HOURS, 300)


class Backoff(unittest.TestCase):
    def test_doubles_and_stops_at_the_cap(self):
        high = mock.Mock(uniform=lambda a, b: b)
        self.assertEqual([live.backoff(n, high) for n in (1, 2, 3)], [30, 60, 120])
        self.assertEqual(live.backoff(50, high), live.BACKOFF[1])

    def test_jitter_shortens_it(self):
        low = mock.Mock(uniform=lambda a, b: a)
        self.assertEqual(live.backoff(1, low), 15)


class Percentile(unittest.TestCase):
    def test_nearest_rank(self):
        v = list(range(1, 101))
        self.assertEqual((live.percentile(v, 50), live.percentile(v, 95)), (50, 95))
        self.assertEqual(live.percentile([7], 95), 7)
        self.assertIsNone(live.percentile([], 50))


class FakeBrowser:
    def __init__(self):
        self.rows = [row("t1", rank=0), row("t2", rank=1)]
        self.block = ""
        self.fetched = []
        self.error = None
        self.closed = self.shown = self.reloads = 0
        self.added = {}
        self.dir = None

    def look(self):
        return {"blocked": self.block, "rows": self.rows}

    def reload(self):
        self.reloads += 1

    def fetch(self, chat, me, out_dir, since=None, no_download=False, minutes=live.FETCH_MINUTES):
        self.fetched.append(chat["thread"])
        self.minutes = minutes
        if self.error:
            raise self.error
        os.makedirs(out_dir, exist_ok=True)
        return {"title": "t", "out_dir": out_dir, "added": 0, "total": 0}

    def show(self):
        self.shown += 1

    def close(self):
        self.closed += 1


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 30, 12, 0)

    def __call__(self):
        return self.t

    def tick(self, seconds=5):
        self.t += dt.timedelta(seconds=seconds)


class Working(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        stop = mock.patch.object(hub, "STOP_FILE", os.path.join(self.dir, "STOP"))
        stop.start()
        self.addCleanup(stop.stop)
        self.cfg = {"me": "Me", "orgs": {"o": {"name": "O", "chats": [
            {"thread": "https://www.facebook.com/messages/t/t1", "name": "One", "slug": "one", "enabled": True},
            {"thread": "https://www.facebook.com/messages/t/t2", "name": "Two", "slug": "two", "enabled": True},
        ]}}, "settings": {"live": True, "quiet": {"start": "23:00", "end": "07:00"}}}
        self.clock = Clock()
        self.b = FakeBrowser()
        self.notices = []
        self.plain = mock.Mock()
        for name, fn in (("hub.chat_dir", lambda c: os.path.join(self.dir, "chats", c["slug"])), ("hub.record_chat", lambda *a: None),
                         ("hubos.process.ingest", lambda *a: {})):
            p = mock.patch(name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.w = self.worker()
        self.w.rest = lambda seconds: None

    def later(self, seconds):
        """Time passes with the worker awake throughout, so it has no reason to reload the list."""
        self.clock.tick(seconds)
        self.w.last_tick = self.clock.t
        self.w.next_refresh = None
        self.w.pacer.next_open = 0

    def worker(self):
        return live.Worker(None, load_config=lambda: self.cfg, notice=self.notice, plain_sync=self.plain, browser=lambda: self.b,
                           folder=self.dir, now=self.clock, rng=random.Random(2))

    def notice(self, text):
        self.notices.append(text)
        return True

    def settle(self):
        """Past the first look and past the pacing gates."""
        self.w.step()
        self.b.fetched.clear()
        self.w.pacer.next_catchup = 1e12
        self.w.pacer.next_open = 0
        self.w.pacer.cool.clear()

    def test_off_by_default_opens_no_browser(self):
        del self.cfg["settings"]["live"]
        self.w.make_browser = mock.Mock()
        self.w.step()
        self.w.make_browser.assert_not_called()
        self.assertEqual(self.w.status()["state"], "stopped")

    def test_turning_it_off_closes_the_browser(self):
        self.w.step()
        self.cfg["settings"]["live"] = False
        self.w.step()
        self.assertEqual(self.b.closed, 1)

    def test_a_changed_row_opens_that_chat_only(self):
        self.settle()
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        self.clock.tick()
        self.w.step()
        self.assertEqual(self.b.fetched, ["https://www.facebook.com/messages/t/t1"])

    def test_a_chat_outside_the_hub_is_never_opened(self):
        self.settle()
        self.b.rows = [row("other", "new", rank=0), row("t1", rank=1), row("t2", rank=2)]
        self.clock.tick()
        self.w.step()
        self.assertEqual(self.b.fetched, [])

    def test_status_shows_live_and_the_last_check(self):
        self.w.step()
        s = self.w.status()
        self.assertEqual((s["state"], s["last_check"], s["why"]), ("live", "2026-10-30T12:00:00", None))

    def test_a_checkpoint_pauses_and_says_why_and_tells_you_once(self):
        self.w.step()
        self.b.block = "/checkpoint/123"
        self.clock.tick()
        self.w.step()
        s = self.w.status()
        self.assertEqual(s["state"], "paused")
        self.assertIn("/checkpoint", s["why"])
        self.assertTrue(s["needs_you"])
        self.assertEqual(self.b.shown, 1)
        self.assertEqual(len(self.notices), 1)

    def test_paused_it_never_touches_the_page_again(self):
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        self.b.look = mock.Mock()
        for _ in range(5):
            self.clock.tick(600)
            self.w.step()
        self.b.look.assert_not_called()

    def test_a_pause_survives_a_restart(self):
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        w = self.worker()
        w.make_browser = mock.Mock()
        w.step()
        w.make_browser.assert_not_called()
        self.assertEqual(w.status()["state"], "paused")

    def test_only_resume_clears_a_pause(self):
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        self.b.block = ""
        self.clock.tick(3600)
        self.w.step()
        self.assertEqual(self.w.status()["state"], "paused")
        self.w.resume()
        self.w.step()
        self.assertEqual(self.w.status()["state"], "live")

    def test_a_block_while_opening_a_chat_pauses(self):
        self.settle()
        self.b.error = scrape.Blocked("/checkpoint/1")
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        self.clock.tick()
        self.w.step()
        self.assertEqual(self.w.status()["state"], "paused")
        self.assertEqual(len(self.b.fetched), 1)

    def test_sync_is_refused_while_paused(self):
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        with self.assertRaises(ValueError):
            self.w.request_sync()

    def test_the_notice_carries_nothing_from_a_chat(self):
        self.cfg["orgs"]["o"]["chats"][0]["name"] = "Secret Chat Name"
        self.w.step()
        self.b.block = "/checkpoint/1"
        self.clock.tick()
        self.w.step()
        self.assertNotIn("Secret", self.notices[0])
        self.assertNotIn("/checkpoint", self.notices[0])

    def test_a_notice_held_back_goes_out_later(self):
        tries = []
        self.w.notice = lambda text: tries.append(text) or len(tries) > 1
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        self.assertEqual(len(tries), 1)
        self.w.step()
        self.assertEqual(len(tries), 2)
        self.w.step()
        self.assertEqual(len(tries), 2)

    def test_a_failed_open_backs_off_and_stays_queued(self):
        self.settle()
        self.b.error = RuntimeError("Timed out waiting for the conversation.")
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        self.clock.tick()
        self.w.step()
        self.assertIn("t1", self.w.pacer.pending)
        self.b.fetched.clear()
        self.clock.tick()
        self.w.step()
        self.assertEqual(self.b.fetched, [], "still backing off")

    def test_chats_that_keep_failing_pause_it(self):
        self.settle()
        self.b.error = RuntimeError("Timed out waiting for the conversation.")
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        for _ in range(live.FAILS_TO_PAUSE):
            self.later(3600)
            self.w.step()
        self.assertEqual(self.w.status()["state"], "paused")

    def test_being_offline_backs_off_without_pausing(self):
        self.settle()
        self.b.error = RuntimeError("net::ERR_INTERNET_DISCONNECTED")
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        for _ in range(live.FAILS_TO_PAUSE + 2):
            self.later(3600)
            self.w.step()
        self.assertEqual(self.w.status()["state"], "live")
        self.assertIn("trying again", self.w.status()["why"])

    def test_an_empty_list_for_two_minutes_pauses(self):
        self.b.rows = []
        for _ in range(live.EMPTY_TO_PAUSE):
            self.w.step()
        self.assertEqual(self.w.status()["state"], "paused")

    def test_quiet_hours_dont_hold_chats(self):
        self.settle()
        self.clock.t = dt.datetime(2026, 10, 30, 23, 30)
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        self.later(0)
        self.w.step()
        self.assertEqual(len(self.b.fetched), 1)
        self.assertIsNone(self.w.status()["why"])

    def test_the_daily_limit_holds_chats_until_tomorrow(self):
        self.settle()
        self.cfg["settings"]["live_limit"] = 5
        self.w.saved["opens"] = 5
        self.b.rows = [row("t1", "new", rank=0), row("t2", rank=1)]
        self.clock.tick()
        self.w.step()
        self.assertEqual(self.b.fetched, [])
        self.assertIn("limit", self.w.status()["why"])
        self.later(24 * 3600)
        self.w.step()
        self.assertEqual(self.w.saved["opens"], 1)

    def test_the_sync_button_doesnt_use_up_the_daily_limit(self):
        self.settle()
        opens = self.w.saved["opens"]
        self.w.request_sync()
        self.clock.tick()
        self.w.step()
        self.assertEqual((len(self.b.fetched), self.w.saved["opens"] - opens, self.w.saved["manual"]), (2, 0, 2))
        self.assertFalse(self.w.syncing)

    def test_a_sync_caught_by_a_pause_doesnt_stay_stuck(self):
        self.w.step()
        self.w.request_sync()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        self.clock.tick()
        self.w.step()
        self.assertFalse(self.w.syncing)

    def test_status_after_resume_isnt_paused_before_the_next_look(self):
        self.w.step()
        self.b.block = "/login"
        self.clock.tick()
        self.w.step()
        self.b.block = ""
        self.w.resume()
        self.assertEqual(self.w.status()["state"], "starting")

    def test_a_browser_that_wont_start_says_so_and_backs_off(self):
        self.w.make_browser = mock.Mock(side_effect=RuntimeError("profile is in use"))
        self.w._fail("profile is in use")
        self.assertIn("trying again", self.w.status()["why"])
        self.w.step()
        self.w.make_browser.assert_not_called()

    def test_stopping_a_queued_sync_never_runs_it(self):
        self.cfg["settings"]["live"] = False
        self.w.request_sync()
        self.w.stop_sync()
        self.assertFalse(self.w.syncing)
        self.w.step()
        self.plain.assert_not_called()

    def test_stopping_the_plain_sync_leaves_no_stop_behind(self):
        self.cfg["settings"]["live"] = False
        self.plain.side_effect = lambda every: (self.w.stop_sync(), self.assertTrue(os.path.exists(hub.STOP_FILE)))
        self.w.request_sync()
        self.w.step()
        self.assertFalse(os.path.exists(hub.STOP_FILE))
        self.assertFalse(self.w.syncing)

    def test_stopping_ends_the_sync_in_the_live_browser(self):
        fetch = self.b.fetch
        self.b.fetch = lambda *a, **k: (self.w.stop_sync(), fetch(*a, **k))[1]
        self.w.request_sync()
        self.clock.tick()
        self.w.step()
        self.assertEqual(len(self.b.fetched), 1)
        self.assertFalse(os.path.exists(hub.STOP_FILE))

    CHAT = {"thread": "https://www.facebook.com/messages/t/1", "slug": "one"}

    def drop(self, reason):
        def fetch(*a, minutes=None, **k):
            self.tries.append(minutes)
            return {"title": "t", "out_dir": self.dir, "added": 0, "total": 0, "dropped": reason}

        self.tries = []
        self.b.fetch = fetch
        self.w.browser = self.b

    def test_a_dropped_chat_from_the_list_goes_back_in_the_queue(self):
        self.drop("stopped")
        self.assertFalse(self.w._open(self.CHAT, "1", "list", self.cfg))
        self.assertIn("1", self.w.pacer.pending)
        self.assertFalse(self.w.backlog)

    def test_a_chat_out_of_time_gets_one_longer_go(self):
        self.drop("limit")
        self.w._open(self.CHAT, "1", "list", self.cfg)
        self.w._open(self.CHAT, "1", "list", self.cfg)
        self.assertEqual(self.tries, [live.FETCH_MINUTES, live.BACKLOG_MINUTES])

    def test_a_chat_that_never_catches_up_pauses_and_says_so(self):
        self.drop("limit")
        self.w._open(self.CHAT, "1", "list", self.cfg)
        self.w._open(self.CHAT, "1", "list", self.cfg)
        self.assertIn("too much to catch up", self.w.saved["paused"])

    def test_catching_up_ends_the_longer_goes(self):
        self.drop("limit")
        self.w._open(self.CHAT, "1", "list", self.cfg)
        self.b = FakeBrowser()
        self.w.browser = self.b
        with mock.patch.object(hub, "record_chat"), mock.patch.object(live.process, "ingest"):
            self.w._open(self.CHAT, "1", "list", self.cfg)
        self.assertEqual(self.b.minutes, live.BACKLOG_MINUTES)
        self.assertFalse(self.w.backlog)

    def test_with_live_off_the_sync_button_runs_the_plain_sync(self):
        self.cfg["settings"]["live"] = False
        self.w.request_sync()
        self.assertTrue(self.w.syncing)
        self.w.step()
        self.plain.assert_called_once_with(False)
        self.assertFalse(self.w.syncing)

    def test_a_full_sync_is_passed_on(self):
        self.cfg["settings"]["live"] = False
        self.w.request_sync(True)
        self.w.step()
        self.plain.assert_called_once_with(True)

    def test_sync_and_list_share_one_browser(self):
        made = []
        self.w.make_browser = lambda: made.append(1) or self.b
        self.w.step()
        self.w.request_sync()
        self.clock.tick()
        self.w.step()
        self.assertEqual(len(made), 1)

    def test_catchup_opens_a_quiet_chat(self):
        self.w.step()
        self.assertEqual(len(self.b.fetched), 1)
        with open(os.path.join(self.dir, "2026-10-30.jsonl"), encoding="utf-8") as f:
            self.assertEqual([json.loads(x)["why"] for x in f if '"open"' in x], ["catchup"])

    def test_a_long_gap_between_ticks_reloads_the_list(self):
        self.w.step()
        self.clock.tick(600)
        self.w.step()
        self.assertEqual(self.b.reloads, 1)

    def test_the_list_gets_reloaded_now_and_then(self):
        self.w.step()
        for _ in range(16):
            self.clock.tick(50 * 60 // 10)
            self.w.last_tick = self.clock.t
            self.w.step()
        self.assertGreaterEqual(self.b.reloads, 1)

    def test_the_log_holds_no_chat_text(self):
        self.cfg["orgs"]["o"]["chats"][0]["name"] = "Secret Chat Name"
        self.w.step()
        with open(os.path.join(self.dir, "2026-10-30.jsonl"), encoding="utf-8") as f:
            self.assertNotIn("Secret", f.read())

    def test_the_comparison_finds_a_message_the_worker_missed(self):
        import common

        def export(folder, keys):
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "messages.jsonl"), "w", encoding="utf-8") as f:
                for k in keys:
                    f.write(json.dumps({"mid": k, "ts": "2026-10-30T11:00", "text": "x"}) + "\n")

        self.settle()
        export(os.path.join(self.dir, "chats", "one"), ["a"])
        export(os.path.join(self.dir, "chats", "two"), ["c"])

        def fetch(chat, me, out, since=None, no_download=False):
            export(out, ["a", "b"] if chat["slug"] == "one" else ["c"])
            return {"title": "t", "out_dir": out, "added": 0, "total": 2}

        self.b.fetch = fetch
        self.w.request_audit()
        self.clock.tick()
        self.w.step()
        with open(os.path.join(self.dir, "audit.json"), encoding="utf-8") as f:
            a = json.load(f)
        self.assertEqual((a["missed"], a["read"]), ({"t1": 1}, 3))
        self.assertFalse(os.listdir(os.path.join(self.dir, "audit")), "no second copy left behind")
        self.assertTrue(common.message_key({"mid": "a"}))


class Reporting(unittest.TestCase):
    def test_delay_opens_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [{"at": "x", "kind": "open", "chat": "1", "why": "list", "lags": [40, 80, 200], "in_list": True},
                    {"at": "x", "kind": "open", "chat": "2", "why": "catchup", "lags": [], "in_list": False},
                    {"at": "y", "kind": "pause", "why": "login", "checkpoint": True},
                    {"at": "y", "kind": "fail"}]
            with open(os.path.join(d, "2026-10-30.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r) for r in rows))
            r = live.report(7, d, dt.date(2026, 10, 30))
        self.assertEqual(r["all_messages"]["messages"], 3)
        self.assertEqual((r["all_messages"]["p50_s"], r["all_messages"]["p95_s"]), (80, 200))
        self.assertEqual(r["all_messages"]["within_2_min"], 0.667)
        self.assertEqual(r["opens_per_day"], {"2026-10-30": {"list": 1, "catchup": 1}})
        self.assertEqual((r["checkpoints"], r["failures"], r["chats_seen_in_list"], r["chats_opened"]), (1, 1, 1, 2))


class StoppingAtLogin(unittest.TestCase):
    URL = "https://www.facebook.com/messages/t/111"

    def page(self, url):
        p = mock.Mock(url=url)
        p.evaluate = mock.Mock(return_value="none")
        return p

    def context(self, logged_in=True):
        return mock.Mock(cookies=lambda: [{"name": "c_user"}] if logged_in else [], pages=[])

    def test_each_kind_of_page_counts(self):
        for path in ("/checkpoint/1", "/login", "/recover/x", "/two_step_verification/two_factor"):
            self.assertEqual(scrape.blocked(self.page("https://www.facebook.com" + path), self.context()), path)

    def test_losing_the_session_counts(self):
        self.assertEqual(scrape.blocked(self.page(self.URL), self.context(False)), "logged out")

    def test_a_normal_chat_doesnt(self):
        self.assertEqual(scrape.blocked(self.page(self.URL), self.context()), "")

    def test_strict_stops_at_once_and_never_navigates(self):
        p = self.page("https://www.facebook.com/checkpoint/1")
        with self.assertRaises(scrape.Blocked):
            scrape.wait_for_chat(self.context(), p, self.URL, 30, strict=True)
        p.goto.assert_not_called()
        p.evaluate.assert_not_called()

    def test_a_chat_that_never_loads_gives_up_after_its_patience(self):
        with self.assertRaises(RuntimeError) as e:
            scrape.wait_for_chat(self.context(), self.page(self.URL), self.URL, 0, strict=True)
        self.assertNotIsInstance(e.exception, scrape.Blocked)


class Notices(unittest.TestCase):
    def setUp(self):
        self.st = mock.Mock(sent_today=lambda p: 0)
        self.cfg = {"settings": {"quiet": {"start": "23:00", "end": "07:00"}, "ping_cap": 5, "alerts": True}}
        with mock.patch.object(botmod.settings, "load_secrets", lambda: {}):
            self.b = botmod.Bot(self.st, lambda: self.cfg)
        self.b.send = mock.Mock(return_value=True)

    def test_sent_outside_quiet_hours(self):
        self.assertTrue(self.b.notice("x", dt.datetime(2026, 10, 30, 12, 0)))
        self.st.mark_sent.assert_called_once()

    def test_held_in_quiet_hours(self):
        self.assertFalse(self.b.notice("x", dt.datetime(2026, 10, 30, 23, 30)))
        self.b.send.assert_not_called()

    def test_held_at_the_ping_cap(self):
        self.st.sent_today = lambda p: 5
        self.assertFalse(self.b.notice("x", dt.datetime(2026, 10, 30, 12, 0)))

    def test_held_when_alerts_are_off(self):
        self.cfg["settings"]["alerts"] = False
        self.assertFalse(self.b.notice("x", dt.datetime(2026, 10, 30, 12, 0)))


if __name__ == "__main__":
    unittest.main()
