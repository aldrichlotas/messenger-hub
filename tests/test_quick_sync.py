"""A sync opens only chats the chat list shows as changed: made-up rows, no browser."""

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import hub  # noqa: E402
import scrape  # noqa: E402

NOW = dt.datetime(2026, 10, 30, 12, 0)
RECENT = "2026-10-30T09:00:00"
LONG_AGO = "2026-10-28T09:00:00"


def row(i, sig="a", weight=500):
    return {"id": i, "sig": sig, "weight": weight, "rank": 0, "lines": 2}


def chat(i):
    return {"thread": f"https://www.facebook.com/messages/t/{i}", "name": i}


def state(*ids, sig="a", weight=500, opened=RECENT):
    return {"rows": {i: {"sig": sig, "weight": weight} for i in ids}, "opened": {i: opened for i in ids}, "tries": {}}


def ids(chats):
    return [hub.thread_id(c["thread"]) for c in chats]


class Picking(unittest.TestCase):
    def pick(self, rows, st, **kw):
        return ids(hub.pick_changed([chat("1"), chat("2")], {r["id"]: r for r in rows}, st, NOW, **kw))

    def test_an_unchanged_row_is_skipped(self):
        self.assertEqual(self.pick([row("1"), row("2")], state("1", "2")), [])

    def test_a_changed_row_is_opened(self):
        self.assertEqual(self.pick([row("1", "b"), row("2")], state("1", "2")), ["1"])

    def test_below_the_unchanged_rows_counts_as_unchanged(self):
        self.assertEqual(self.pick([row("1")], state("1", "2")), [])

    def test_a_chat_never_opened_is_opened(self):
        self.assertEqual(self.pick([row("1"), row("2")], state("1")), ["2"])

    def test_a_chat_too_deep_to_have_a_row_is_skipped_once_opened(self):
        st = state("1")
        st["opened"]["2"] = RECENT
        self.assertEqual(self.pick([row("1")], st), [])

    def test_that_chat_opens_when_a_message_brings_it_up_the_list(self):
        st = state("1")
        st["opened"]["2"] = RECENT
        self.assertEqual(self.pick([row("2"), row("1")], st), ["2"])

    def test_that_chat_still_opens_once_a_day(self):
        st = state("1")
        st["opened"]["2"] = LONG_AGO
        self.assertEqual(self.pick([row("1")], st), ["2"])

    def test_turning_bold_with_the_same_text_is_opened(self):
        self.assertEqual(self.pick([row("1", weight=700), row("2")], state("1", "2")), ["1"])

    def test_reading_on_the_phone_isnt_a_change(self):
        self.assertEqual(self.pick([row("1"), row("2")], state("1", "2", weight=700)), [])

    def test_every_chat_gets_opened_once_a_day(self):
        st = state("1", "2")
        st["opened"]["2"] = LONG_AGO
        self.assertEqual(self.pick([row("1"), row("2")], st), ["2"])

    def test_a_full_sync_opens_everything(self):
        self.assertEqual(self.pick([row("1"), row("2")], state("1", "2"), every_chat=True), ["1", "2"])

    def test_a_list_it_couldnt_read_opens_everything(self):
        self.assertEqual(self.pick([], state("1", "2")), ["1", "2"])


class FakeList:
    """The list as it mounts: a few rows at a time as it's scrolled."""

    def __init__(self, rows, per_screen=3):
        self.rows, self.per, self.at, self.screens = rows, per_screen, 0, 1

    def evaluate(self, js):
        if "drain" in js:
            return self.rows[self.at: self.at + self.per]
        self.at += self.per
        self.screens += 1
        return {"ok": True, "end": self.at >= len(self.rows)}


class Reading(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(hub.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_stops_at_a_run_of_unchanged_rows(self):
        rows = [row(str(n)) for n in range(100)]
        known = {r["id"]: {"sig": "a", "weight": 500} for r in rows}
        page = FakeList([row("x", "new")] + rows)
        got = hub.read_rows(page, {"80"}, known)
        self.assertIn("x", got)
        self.assertLess(len(got), 12)

    def test_a_changed_row_restarts_the_count(self):
        known = {str(n): {"sig": "a", "weight": 500} for n in range(20)}
        rows = [row(str(n), "new" if n == 4 else "a") for n in range(20)]
        got = hub.read_rows(FakeList(rows), {"99"}, known)
        self.assertIn("4", got)
        self.assertGreaterEqual(len(got), 10)

    def test_with_nothing_known_it_stops_once_all_wanted_chats_are_seen(self):
        page = FakeList([row(str(n)) for n in range(100)])
        got = hub.read_rows(page, {"1", "7"}, {})
        self.assertIn("7", got)
        self.assertLess(page.screens, 5)

    def test_stops_at_the_end_of_a_short_list(self):
        page = FakeList([row("1")])
        self.assertEqual(list(hub.read_rows(page, {"99"}, {})), ["1"])
        self.assertLess(page.screens, hub.SKIP_ROUNDS)

    def test_a_slow_list_isnt_taken_for_the_end(self):
        class Slow(FakeList):
            def evaluate(self, js):
                got = super().evaluate(js)
                if "drain" in js and 3 <= self.screens <= 12:
                    return []
                return {**got, "end": False} if "step" in js else got

        rows = [row(str(n)) for n in range(60)]
        self.assertIn("50", hub.read_rows(Slow(rows), {"50"}, {}))

    def test_a_list_that_never_loads_gives_up(self):
        page = mock.Mock()
        page.evaluate.side_effect = lambda js: [] if "drain" in js else {"ok": False, "end": False}
        self.assertEqual(hub.read_rows(page, {"1"}, {}), {})

    def test_never_reads_past_its_limit(self):
        page = FakeList([row(str(n)) for n in range(5000)])
        hub.read_rows(page, {"nope"}, {})
        self.assertLessEqual(page.screens, hub.SKIP_ROUNDS + 1)  # it starts on the first screen


class Noting(unittest.TestCase):
    def test_new_messages_mark_the_row_as_seen(self):
        st = state("1")
        hub.note_opened(st, "1", row("1", "b"), 3, NOW)
        self.assertEqual(st["rows"]["1"]["sig"], "b")
        self.assertEqual(st["opened"]["1"], NOW.isoformat(timespec="seconds"))

    def test_a_first_visit_with_nothing_new_is_the_baseline(self):
        st = state()
        hub.note_opened(st, "1", row("1"), 0, NOW)
        self.assertEqual(st["rows"]["1"]["sig"], "a")

    def test_a_changed_row_with_nothing_new_gets_a_second_look(self):
        st = state("1")
        hub.note_opened(st, "1", row("1", "b"), 0, NOW)
        self.assertEqual(st["rows"]["1"]["sig"], "a")
        self.assertEqual(ids(hub.pick_changed([chat("1")], {"1": row("1", "b")}, st, NOW)), ["1"])

    def test_the_second_look_ends_it(self):
        st = state("1")
        hub.note_opened(st, "1", row("1", "b"), 0, NOW)
        hub.note_opened(st, "1", row("1", "b"), 0, NOW)
        self.assertEqual(st["rows"]["1"]["sig"], "b")
        self.assertEqual(st["tries"], {})

    def test_a_chat_that_failed_stays_changed(self):
        st = state("1")  # note_opened isn't called for a chat that failed
        self.assertEqual(ids(hub.pick_changed([chat("1")], {"1": row("1", "b")}, st, NOW)), ["1"])

    def test_a_chat_the_list_didnt_show_still_counts_as_opened(self):
        st = state("1")
        hub.note_opened(st, "1", None, 0, NOW)
        self.assertEqual(st["rows"]["1"]["sig"], "a")
        self.assertEqual(st["opened"]["1"], NOW.isoformat(timespec="seconds"))

    def test_other_chats_rows_are_kept_as_markers(self):
        st = state()
        hub.note_other_rows(st, {"other": row("other"), "1": row("1", "b")}, [chat("1")])
        self.assertEqual(list(st["rows"]), ["other"])


class Stopping(unittest.TestCase):
    MSG = {"kind": "message", "id": 1, "ts": "2026-10-30T09:00", "sender": "Sam", "text": "hi"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.stop = os.path.join(self.dir, "STOP")
        for p in (mock.patch.object(hub, "STOP_FILE", self.stop), mock.patch.object(scrape, "STOP_FILE", self.stop),
                  mock.patch.object(hub, "LIST_STATE", os.path.join(self.dir, "s.json")), mock.patch.object(hub, "DATA", self.dir),
                  mock.patch.object(hub.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def press(self):
        open(self.stop, "w").close()

    def sync(self, fake, chats=("1", "2")):
        args = mock.Mock(every_chat=True, since=None, no_download=True, max_minutes=1, debug=False)
        context = mock.Mock(pages=[])
        with mock.patch("playwright.sync_api.sync_playwright"), mock.patch.object(scrape, "launch", lambda p: context), \
                mock.patch.object(scrape, "scrape_thread", fake), mock.patch.object(hub, "sync_targets", lambda c, k: [chat(i) for i in chats]), \
                mock.patch.object(hub, "look_at_list", lambda *a: {}), mock.patch.object(hub, "record_chat") as recorded:
            hub.sync({"me": "Aldrich"}, [], args)
        return recorded

    def test_stopping_opens_no_more_chats_and_clears_itself(self):
        seen = []

        def fake(context, thread, **kw):
            seen.append(thread)
            self.press()
            return {"title": "t", "out_dir": self.dir, "added": 1, "total": 1}

        self.assertEqual(self.sync(fake).call_count, 1)
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(self.stop))

    def test_a_dropped_chat_stays_changed(self):
        def fake(context, thread, **kw):
            self.press()
            return {"title": "t", "out_dir": self.dir, "added": 0, "total": 5, "dropped": True}

        self.assertEqual(self.sync(fake).call_count, 0)
        self.assertEqual(hub.load_list_state()["opened"], {})

    def test_a_stop_during_the_list_read_ends_it(self):
        self.press()
        page = FakeList([row(str(n)) for n in range(100)])
        self.assertLessEqual(len(hub.read_rows(page, {"nope"}, {})), page.per)

    def scrape_once(self, existing, stop=True, step=None, fresh_ts="2026-10-31T09:00", **kw):
        page = mock.Mock()
        step = step or {"ok": True, "added": 1, "total": 1, "scrollHeight": 1}
        page.evaluate.side_effect = lambda js: step if "step" in js else [] if "dump" in js else "t"
        if stop:
            self.press()
        with mock.patch.object(scrape, "wait_for_chat", lambda c, p, *a: p), mock.patch.object(scrape, "settle", lambda p: None), \
                mock.patch.object(scrape, "to_messages", lambda *a: [dict(self.MSG, id=9, ts=fresh_ts, text="new")]), \
                mock.patch.object(scrape.common, "load_messages", lambda d: list(existing)), \
                mock.patch.object(scrape.common, "write_export") as wrote:
            res = scrape.scrape_thread(mock.Mock(), "1", out_dir=self.dir, no_download=True, page=page, **kw)
        return res, wrote

    def test_a_chat_already_synced_saves_nothing_when_stopped(self):
        res, wrote = self.scrape_once([self.MSG])
        self.assertEqual(res["dropped"], "stopped")
        wrote.assert_not_called()

    def test_a_chat_already_synced_saves_nothing_out_of_time(self):
        res, wrote = self.scrape_once([self.MSG], stop=False, max_minutes=-1)
        self.assertEqual(res["dropped"], "limit")
        wrote.assert_not_called()

    def test_reaching_the_bookmark_on_the_last_moment_saves(self):
        res, wrote = self.scrape_once([self.MSG], stop=False, fresh_ts="2026-10-29T09:00", max_minutes=-1)
        self.assertNotIn("dropped", res)
        wrote.assert_called_once()

    def test_a_chat_that_stops_loading_is_a_failure_not_a_save(self):
        with self.assertRaises(RuntimeError):
            self.scrape_once([self.MSG], stop=False, step={"ok": False, "reason": "gone"})

    def test_a_new_chat_keeps_what_it_read_out_of_time(self):
        res, wrote = self.scrape_once([], stop=False, max_minutes=-1)
        self.assertEqual(res["added"], 1)
        wrote.assert_called_once()

    def test_a_new_chat_keeps_what_it_read_when_stopped(self):
        res, wrote = self.scrape_once([])
        self.assertEqual(res["added"], 1)
        wrote.assert_called_once()
        self.assertTrue(os.path.exists(self.stop))  # left for the sync, which stops the other chats

    def test_a_stop_ends_the_wait_at_a_login_page(self):
        self.press()
        with self.assertRaises(RuntimeError):
            scrape.wait_for_chat(mock.Mock(pages=[]), mock.Mock(), "https://www.facebook.com/messages/t/1")


class NotesChat(unittest.TestCase):
    CFG = {"orgs": {}, "profile": {"self_chat": "https://www.facebook.com/messages/t/9", "self_slug": None}}

    def test_its_first_sync_reads_back_a_month(self):
        c = hub.sync_targets(self.CFG, [])[0]
        self.assertEqual(c["since"], (dt.date.today() - dt.timedelta(days=hub.NOTES_DAYS)).isoformat())

    def test_later_syncs_pick_up_from_the_export(self):
        cfg = {**self.CFG, "profile": {**self.CFG["profile"], "self_slug": "notes"}}
        self.assertIsNone(hub.sync_targets(cfg, [])[0]["since"])


class Saving(unittest.TestCase):
    def test_state_round_trips_and_holds_no_text(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hub, "LIST_STATE", os.path.join(d, "s.json")), mock.patch.object(hub, "DATA", d):
            self.assertEqual(hub.load_list_state(), {"rows": {}, "opened": {}, "tries": {}})
            st = state("1")
            hub.save_list_state(st)
            self.assertEqual(hub.load_list_state(), st)
            with open(hub.LIST_STATE, encoding="utf-8") as f:
                self.assertEqual(set(json.load(f)), {"rows", "opened", "tries"})


class Settling(unittest.TestCase):
    def counts(self, seq):
        page = mock.Mock()
        page.evaluate.side_effect = list(seq) + [seq[-1]] * 50
        return page

    def test_quick_when_nothing_is_drawing(self):
        clock = iter(float(n) * 0.3 for n in range(100))
        with mock.patch.object(scrape.time, "time", lambda: next(clock)), mock.patch.object(scrape.time, "sleep", lambda s: None):
            page = self.counts([20])
            scrape.settle(page)
        self.assertLess(page.evaluate.call_count, 7)

    def test_waits_while_messages_keep_arriving(self):
        clock = iter(float(n) * 0.3 for n in range(100))
        with mock.patch.object(scrape.time, "time", lambda: next(clock)), mock.patch.object(scrape.time, "sleep", lambda s: None):
            page = self.counts([5, 8, 11, 14, 17, 20, 20, 20])
            scrape.settle(page)
        self.assertGreaterEqual(page.evaluate.call_count, 6)

    def test_gives_up_at_its_limit(self):
        clock = iter(float(n) * 0.3 for n in range(100))
        with mock.patch.object(scrape.time, "time", lambda: next(clock)), mock.patch.object(scrape.time, "sleep", lambda s: None):
            page = self.counts(list(range(50)))
            scrape.settle(page)
        self.assertLess(page.evaluate.call_count, 15)


class RecordChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patch = mock.patch.object(hub, "CONFIG", os.path.join(self.tmp, "orgs.json"))
        patch.start()
        self.addCleanup(patch.stop)
        hub.save_config({"me": "A", "orgs": {"o": {"chats": [{"thread": chat("1")["thread"]}]}}, "settings": {}})

    def test_a_setting_changed_during_a_sync_survives(self):
        at_start = hub.load_config()
        changed = hub.load_config()
        changed["settings"]["live"] = True
        hub.save_config(changed)
        hub.record_chat(at_start, chat("1"), {"out_dir": os.path.join("chats", "one"), "title": "One"})
        saved = hub.load_config()
        self.assertTrue(saved["settings"]["live"])
        self.assertEqual(saved["orgs"]["o"]["chats"][0]["slug"], "one")
        self.assertEqual(at_start["orgs"]["o"]["chats"][0]["slug"], "one")


if __name__ == "__main__":
    unittest.main()
