"""ui/app.html in a headless browser: keyboard, focus, forced colours, reflow and axe (made-up data, no hub needed).

Serves the page from a throwaway local server with a synthetic /api/app, so nothing touches data/ or the
running hub. Skipped when Playwright or its Chromium isn't installed. The axe test loads axe-core from
cdnjs and is skipped when that can't be reached.
"""

import base64
import datetime as dt
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "app.html")
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
NOW = dt.datetime.now().replace(microsecond=0)


def ts(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


def line(author, text, ago, role="ctx", media=(), is_me=False):
    return {"author": author, "is_me": is_me, "text": text, "ts": ts(ago), "ref": "eb#1", "role": role,
            "media": list(media), "links": [], "unsaved": False}


def card(key, state, asker, label, media=(), handled=None, unread=False, links=(), asks=("Can you check this?",), why=None):
    lines = [line(asker, "Can you check this?", 30, "ping", media), line("Bea", "ok", 25)]
    return {"key": key, "channel": "https://example.invalid/t/1", "org": "ecg", "chat_name": "[ECG] EB", "label": label,
            "state": state, "why": why, "gist": " ".join(asks), "asks": list(asks), "due": None, "handled": handled, "media": list(media),
            "media_count": len(media), "links": list(links), "asker": asker, "above": False, "pings": 1, "ping_items": [1],
            "open_items": [], "last_ping": ts(30), "start": ts(40), "end": ts(25), "size": 2, "unread": unread,
            "excerpt": lines, "full": lines, "summary": None, "hash": key}


IMG_A = {"type": "image", "url": "/media/eb/attachments/a.png"}
IMG_B = {"type": "image", "url": "/media/eb/attachments/b.png"}
VID = {"type": "video", "url": "/media/eb/attachments/v.mp4"}
CARDS = [  # not in screen order on purpose: the hub sends them by recency, the list groups them by state
    card("k-fyi", "fyi", "Cy", "fyi", [VID]),
    card("k-needs", "needs", "Sam", "review", [IMG_A, IMG_B], unread=True, links=[{"url": "https://example.com/doc", "site": "example.com"}],
         asks=("Can you check this?", "the pubmat", "naw")),
    card("k-answered", "answered", "Bea", "question", handled={"how": "you replied", "ts": ts(20)}),
    card("k-follow", "followup", "Dee", "request", handled={"how": "you replied", "ts": ts(15)}, why="you said you'd get to it"),
]


def row(key, tier, say, card=None, entry=None, title="Aldrich, send the deck", **f):
    return {"key": key, "entry": entry, "card": card, "kind": "task", "title": title, "due": None, "due_has_time": 0, "org": "ecg", "who": "Sam", "chat": "[ECG] EB",
            "tier": tier, "say": say, "unsure": False, "source": None, "origin": "rule", **f}


def suggestion(i, title):
    return {"id": i, "kind": "deadline", "title": title, "due": None, "due_has_time": 0, "org": "ecg", "chat_name": "[ECG] EB", "author": "Sam", "source_text": title,
            "reasons": "from Sam", "ref": f"eb#{i}", "origin": "rule", "review": "unreviewed", "confidence": .7}


PLAN = {"now": [row("e7", 0, "Overdue", card="k-needs", entry=7), row("ck-follow", 4, "you said you'd get to it", card="k-follow")],
        "next": [row("e8", 5, "To do", entry=8, title="GA on Friday")],
        "now_count": 2, "next_count": 1, "dealt": [{"title": "Can you check this?", "how": "you marked it done", "ts": ts(20), "org": "ecg", "who": "Bea", "card": "k-answered", "undo": True}], "dealt_count": 1}
APP = {
    "synced_at": NOW.isoformat(),
    "dash": {"now": NOW.isoformat(), "today": [], "tomorrow": [], "overdue": [], "deadlines": [], "inbox": [], "notes": [], "review": [], "review_expired": [],
             "review_expired_count": 0, "plan": PLAN, "brief": {"orgs": [{"org": "ecg", "messages": 12, "pings": 2, "waiting": 1, "answered": 1}], "found": []},
             "review_count": 0, "cards": CARDS, "unread_mentions": 1, "needs_you": 1, "wins": [], "last_process": NOW.isoformat()},
    "me": "Aldrich", "profile": {"names": ["Aldrich"], "self_chat": None, "self_slug": None},
    "settings": {"briefs": {"morning": {"on": True, "at": "07:00"}, "afternoon": {"on": False, "at": "13:00"}, "night": {"on": True, "at": "21:00"}},
                 "quiet": {"start": "23:00", "end": "07:00"}, "alerts": True, "ping_cap": 8, "detail": "short", "process_minutes": 10, "summaries": "off"},
    "orgs": {"ecg": {"name": "ECG", "my_title": None, "aliases": None, "committees": None, "above": None, "chats": 1}},
    "selfCandidates": [{"name": "Notes", "url": "https://example.invalid/t/2", "preview": ""}],
    "telegram": {"from_env": [], "has_token": False, "paired": False, "pair_code": "123456", "bot_name": "", "error": None},
    "startup": False, "desktopShortcut": False, "syncing": False,
}


POSTS = []  # (path, body) the page sent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/bar"):
            with open(UI, "rb") as f:
                return self.send(f.read(), "text/html; charset=utf-8")
        if path == "/api/app":
            return self.send(json.dumps(APP).encode(), "application/json")
        if path.endswith(".png"):
            return self.send(PNG, "image/png")
        if path.endswith(".mp4"):
            return self.send(b"", "video/mp4")
        self.send(b"not found", "text/plain", 404)

    def do_POST(self):
        POSTS.append((self.path, self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
        self.send(json.dumps(APP).encode(), "application/json")


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.server.server_address[1]
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:  # pragma: no cover
            cls.pw.stop()
            cls.server.shutdown()
            raise unittest.SkipTest("Chromium for Playwright isn't installed: %s" % e)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()

    def page(self, width=1360, scheme="light", forced=None):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme, forced_colors=forced or "none")
        self.addCleanup(ctx.close)
        p = ctx.new_page()
        p.goto(self.url)
        p.wait_for_selector("#tabs button")
        return p

    def live(self, **state):
        APP["live"] = state
        self.addCleanup(APP.pop, "live", None)

    def syncing(self, stopping=False):
        APP.update(syncing=True, stopping=stopping)
        self.addCleanup(APP.update, syncing=False, stopping=False)

    def test_sync_turns_into_stop_while_it_runs(self):
        self.syncing()
        del POSTS[:]
        p = self.page()
        self.assertEqual(p.inner_text("#syncbtn"), "Stop sync")
        self.assertTrue(p.is_enabled("#syncbtn"))
        p.click("#syncbtn")
        p.wait_for_timeout(100)
        self.assertEqual([x[0] for x in POSTS if x[0] != "/api/app/event"], ["/api/app/sync-stop"])

    def test_stopping_shows_and_cant_be_pressed_twice(self):
        self.syncing(stopping=True)
        p = self.page()
        self.assertEqual(p.inner_text("#syncbtn"), "Stopping…")
        self.assertTrue(p.is_disabled("#syncbtn"))

    def test_the_button_goes_back_when_a_sync_ends(self):
        self.syncing()
        p = self.page()
        APP.update(syncing=False)
        p.wait_for_function("document.querySelector('#syncbtn').textContent === 'Sync'", timeout=5000)

    def test_live_sync_starts_off_and_says_stopped(self):
        p = self.page()
        p.click('[data-tab="settings"]')
        self.assertFalse(p.is_checked("#livesync"))
        self.assertIn("Stopped", p.inner_text("#livestate"))
        self.assertIn("Synced", p.inner_text("#livepill"))

    def test_the_live_switch_saves_the_setting(self):
        del POSTS[:]
        p = self.page()
        p.click('[data-tab="settings"]')
        p.click("#livesync")  # a real mouse click: it fails if the checkbox can't be hit where the switch shows
        p.wait_for_timeout(100)
        self.assertIn(("/api/app/settings", '{"live":true}'), POSTS)

    def test_a_mouse_click_on_any_switch_reaches_its_checkbox(self):
        p = self.page()
        p.click('[data-tab="settings"]')
        hit = p.eval_on_selector_all(".switch", """els => els.map(e => {
            e.scrollIntoView({ block: "center" });
            const s = e.getBoundingClientRect();
            return document.elementFromPoint(s.x + s.width / 2, s.y + s.height / 2) === e.querySelector("input");
        })""")
        self.assertGreaterEqual(len(hit), 5)
        self.assertTrue(all(hit), hit)

    def test_live_shows_the_last_check_and_opens_today(self):
        self.live(state="live", last_check=NOW.isoformat(), opens_today=4, limit=300, why=None, needs_you=False)
        p = self.page()
        p.click('[data-tab="settings"]')
        text = p.inner_text("#livestate")
        self.assertIn("Live", text)
        self.assertIn(NOW.isoformat()[11:19], text)
        self.assertIn("4 of 300", text)
        self.assertTrue(p.inner_text("#livepill").startswith("Live ·"))
        self.assertEqual(p.locator("#liveresume").count(), 0)

    def test_live_doesnt_look_current_while_changed_chats_wait(self):
        self.live(state="live", last_check=NOW.isoformat(), opens_today=300, limit=300, why="Daily limit reached: chats that change wait until tomorrow", needs_you=False, waiting=5)
        APP["dash"]["plan"], plan = {"now": [], "next": [], "now_count": 0, "next_count": 0, "dealt": [], "dealt_count": 0}, APP["dash"]["plan"]
        self.addCleanup(APP["dash"].update, plan=plan)
        p = self.page()
        self.assertEqual(p.inner_text("#livepill"), "Daily limit reached · 5 waiting")
        self.assertIn("warn", p.get_attribute("#livepill", "class"))
        self.assertIn("may be behind", p.inner_text(".empty"))
        p.click('[data-tab="settings"]')
        self.assertIn("5 waiting", p.inner_text("#livestate"))

    def test_paused_says_why_on_every_tab_and_offers_resume(self):
        self.live(state="paused", last_check=NOW.isoformat(), opens_today=4, limit=300, why="Facebook wants you to sign in", needs_you=True)
        del POSTS[:]
        p = self.page()
        self.assertEqual(p.inner_text("#livepill"), "Live sync paused")
        p.click('[data-tab="settings"]')
        self.assertIn("Facebook wants you to sign in", p.inner_text("#livestate"))
        p.click("#liveresume")
        p.wait_for_timeout(100)
        self.assertEqual(POSTS[-1][0], "/api/app/live-resume")

    def test_a_pause_that_clears_itself_offers_no_resume(self):
        self.live(state="live", last_check=NOW.isoformat(), opens_today=300, limit=300, why="Daily limit reached", needs_you=False)
        p = self.page()
        p.click('[data-tab="settings"]')
        self.assertIn("Daily limit", p.inner_text("#livestate"))
        self.assertEqual(p.locator("#liveresume").count(), 0)

    def mentions(self, p):
        p.click('[data-tab="mentions"]')
        p.wait_for_selector(".trow")

    def active(self, p, js):
        return p.evaluate("() => { const a = document.activeElement; return (%s); }" % js)

    # --- the full-size viewer -------------------------------------------------------------------------
    def test_viewer_focus(self):
        p = self.page()
        self.mentions(p)
        p.focus(".dshared [data-lb]")
        p.keyboard.press("Enter")
        self.assertTrue(self.active(p, "!!a.closest('.lb') && a.tagName === 'BUTTON'"), "focus starts on Close")
        self.assertEqual(p.get_attribute(".lb", "aria-modal"), "true")
        self.assertTrue(p.evaluate("$('header').inert && !!$('#main > [inert]')"), "the page behind is inert")
        for key in ("Tab", "Shift+Tab", "j"):
            p.keyboard.press(key)
            self.assertTrue(self.active(p, "!!a.closest('.lb')"), key + " stays inside the viewer")
        self.assertEqual(p.get_attribute(".trow.sel", "data-sel"), "k-needs", "j waits while the viewer is open")
        p.keyboard.press("Escape")
        self.assertEqual(p.locator(".lb").count(), 0)
        self.assertTrue(self.active(p, "a === document.querySelector('.dshared [data-lb]')"), "Esc returns to the thumbnail")

        # the same photo also sits in the conversation: focus goes back to that copy
        p.focus(".lines [data-lb]")
        p.keyboard.press(" ")
        p.keyboard.press("Enter")  # on Close
        self.assertEqual(p.locator(".lb").count(), 0)
        self.assertTrue(self.active(p, "!!a.closest('.lines') && a.dataset.lb.endsWith('a.png')"), "Close returns to the copy that opened it")

        p.click(".dshared [data-lb] >> nth=1")
        p.mouse.click(5, 450)  # backdrop
        self.assertEqual(p.locator(".lb").count(), 0)
        self.assertTrue(self.active(p, "a === document.querySelectorAll('.dshared [data-lb]')[1]"), "backdrop click returns focus")

    def test_viewer_video_tab_wraps(self):
        p = self.page()
        self.mentions(p)
        p.click('.trow[data-sel="k-fyi"]')
        p.focus(".dshared video[data-lb]")
        p.keyboard.press("Enter")
        self.assertTrue(self.active(p, "a.tagName === 'BUTTON' && !!a.closest('.lb')"))
        p.keyboard.press("Tab")
        self.assertTrue(self.active(p, "a.tagName === 'VIDEO' && !!a.closest('.lb')"), "Tab from Close wraps to the video")

    # --- the conversation list ------------------------------------------------------------------------
    def test_asks_shown_as_separate_messages(self):
        p = self.page()
        self.mentions(p)
        self.assertEqual(p.locator(".dasks li").all_inner_texts(), ["Can you check this?", "the pubmat", "naw"])
        self.assertEqual(p.locator('.trow[data-sel="k-needs"] .gist .sep').count(), 2)
        p.click('.trow[data-sel="k-answered"]')  # one message: plain gist, no list
        self.assertEqual(p.locator(".dasks").count(), 0)
        self.assertEqual(p.inner_text(".dgist"), "Can you check this?")

    def test_rows_wide(self):
        p = self.page()
        self.mentions(p)
        sel = lambda: p.get_attribute('.trow[aria-selected="true"]', "data-sel")
        self.assertEqual(p.get_attribute(".tlist", "role"), "listbox")
        self.assertEqual(sel(), "k-needs")
        p.focus(".trow.sel")
        p.keyboard.press("j")
        self.assertEqual(sel(), "k-follow", "Follow up comes right after Needs you")
        self.assertTrue(self.active(p, "a.dataset.sel === 'k-follow'"), "focus follows the selection")
        p.keyboard.press("k")
        self.assertEqual(sel(), "k-needs")
        p.keyboard.press("End")
        self.assertEqual(sel(), "k-fyi")
        p.keyboard.press("Home")
        self.assertEqual(sel(), "k-needs")
        p.keyboard.press("Enter")
        self.assertTrue(self.active(p, "a.id === 'dlines'"), "Enter moves into the conversation")
        p.keyboard.press("ArrowDown")
        self.assertEqual(sel(), "k-needs", "arrows scroll the conversation instead of changing rows")

    def test_rows_narrow(self):
        p = self.page(width=430)
        self.mentions(p)
        row = '.trow[data-sel="k-needs"]'
        self.assertEqual(p.get_attribute(row, "role"), "button")
        self.assertEqual(p.get_attribute(row, "aria-expanded"), "true")
        self.assertEqual(p.locator("#" + p.get_attribute(row, "aria-controls")).count(), 1)
        p.focus(row)
        p.keyboard.press(" ")
        self.assertEqual(p.get_attribute(row, "aria-expanded"), "false")
        self.assertTrue(self.active(p, "a.dataset.sel === 'k-needs'"), "a collapsed row keeps focus")
        p.keyboard.press("Enter")
        self.assertEqual(p.get_attribute(row, "aria-expanded"), "true")

    # --- Now ---------------------------------------------------------------------------------------------
    def test_one_thing_leads_and_your_name_is_gone(self):
        p = self.page()
        self.assertEqual(p.locator(".nrow.hero").count(), 1)
        self.assertEqual(p.inner_text(".nrow.hero .ntitle"), "Send the deck")
        self.assertEqual(p.locator(".nrow").count(), 3, "each commitment once: two now, one coming up")

    def test_dealt_with_is_on_screen_and_can_be_undone(self):
        p = self.page()
        self.assertIn("you marked it done", p.inner_text(".drow"))
        del POSTS[:]
        p.click("[data-reopen]")
        p.wait_for_timeout(100)
        self.assertIn(("/api/app/reopen", '{"card":"k-answered"}'), POSTS)

    def test_done_closes_the_entry_and_the_conversation_together(self):
        p = self.page()
        del POSTS[:]
        p.click('.nrow.hero [data-res="done"]')
        p.wait_for_timeout(100)
        self.assertIn(("/api/app/resolve", '{"how":"done","entry":7,"card":"k-needs"}'), POSTS)

    def test_now_works_from_the_keyboard(self):
        p = self.page()
        p.focus(".nrow.hero")
        p.keyboard.press("j")
        self.assertTrue(self.active(p, "a.dataset.n === 'ck-follow'"))
        p.keyboard.press("Enter")
        self.assertEqual(p.locator('.nrow[data-n="ck-follow"] .nmore').count(), 1)
        del POSTS[:]
        p.keyboard.press("x")
        p.wait_for_timeout(100)
        self.assertIn(("/api/app/resolve", '{"how":"drop","entry":null,"card":"k-follow"}'), POSTS)

    def test_read_the_conversation_goes_to_it(self):
        p = self.page()
        p.click(".nrow.hero [data-nopen]")
        p.wait_for_selector(".tlist")
        self.assertEqual(p.get_attribute(".trow.sel", "data-sel"), "k-needs")

    def test_status_says_how_fresh_on_every_tab(self):
        old = (NOW - dt.timedelta(days=3)).isoformat()
        self.addCleanup(APP.update, synced_at=NOW.isoformat())
        for synced, want, cls in ((NOW.isoformat(), "Synced", "ok"), ((NOW - dt.timedelta(hours=8)).isoformat(), "Synced 8h ago", "warn"), (old, "Not synced for 3d", "bad"), (None, "Never synced", "bad")):
            APP["synced_at"] = synced
            p = self.page()
            for t in ("home", "review", "mentions", "notes", "settings"):
                p.click('[data-tab="%s"]' % t)
                self.assertIn(want, p.inner_text("#livepill"), t)
                self.assertIn(cls, p.get_attribute("#livepill", "class"))

    def test_an_empty_list_doesnt_claim_all_clear_when_stale(self):
        APP["synced_at"], plan = (NOW - dt.timedelta(days=3)).isoformat(), APP["dash"]["plan"]
        APP["dash"]["plan"] = {"now": [], "next": [], "now_count": 0, "next_count": 0, "dealt": [], "dealt_count": 0}
        self.addCleanup(APP.update, synced_at=NOW.isoformat())
        self.addCleanup(APP["dash"].update, plan=plan)
        self.assertIn("may be behind", self.page().inner_text(".empty"))

    def test_notes_stay_off_now_and_open_from_the_header(self):
        p = self.page()
        self.assertTrue(p.is_hidden("#quick"))
        p.keyboard.press("n")
        self.assertTrue(p.is_visible("#quick"))
        p.keyboard.press("Escape")
        self.assertTrue(p.is_hidden("#quick"))
        p.click('[data-tab="notes"]')
        self.assertEqual(p.locator("#notetext").count(), 1)

    # --- the floating bar --------------------------------------------------------------------------------
    def bar(self, scheme="light"):
        ctx = self.browser.new_context(viewport={"width": 640, "height": 48}, color_scheme=scheme)
        self.addCleanup(ctx.close)
        p = ctx.new_page()
        p.goto(self.url + "bar")
        p.wait_for_selector(".barline")
        return p

    def test_the_bar_shows_status_and_the_next_thing_on_one_line(self):
        p = self.bar()
        self.assertEqual(p.inner_text(".bartitle"), "Send the deck")
        self.assertGreater(p.evaluate("document.querySelector('.barnext').offsetWidth"), 200, "the title has room, not just text")
        self.assertIn("Synced", p.inner_text(".barline .livepill"))
        self.assertIn("1 more", p.inner_text("#barmore"))
        self.assertTrue(p.is_hidden("header"))
        self.assertLessEqual(p.evaluate("document.documentElement.scrollHeight"), 48, "nothing spills out of the strip")

    def test_a_long_title_stays_on_one_line(self):
        plan = APP["dash"]["plan"]
        APP["dash"]["plan"] = {**plan, "now": [row("e7", 0, "Overdue", entry=7, title="word " * 60)]}
        self.addCleanup(APP["dash"].update, plan=plan)
        p = self.bar()
        self.assertLessEqual(p.evaluate("document.documentElement.scrollWidth"), 640)
        self.assertLessEqual(p.evaluate("document.querySelector('.bartitle').offsetHeight"), 24)

    def test_done_in_the_bar_is_the_same_done(self):
        p = self.bar()
        del POSTS[:]
        p.click('.barline [data-res="done"]')
        p.wait_for_timeout(100)
        self.assertIn(("/api/app/resolve", '{"how":"done","entry":7,"card":"k-needs"}'), POSTS)

    def test_the_bar_opens_to_the_rest(self):
        p = self.bar()
        p.set_viewport_size({"width": 640, "height": 440})
        p.click("#barmore")
        self.assertEqual(p.get_attribute("#barmore", "aria-expanded"), "true")
        self.assertEqual(p.locator(".barlist .nrow").count(), 1)
        self.assertEqual(p.locator("#notetext").count(), 1)

    def test_the_bar_passes_axe(self):
        for scheme in ("light", "dark"):
            p = self.bar(scheme)
            try:
                p.add_script_tag(url=AXE)
            except Exception as e:  # pragma: no cover
                self.skipTest("axe-core unreachable: %s" % e)
            self.assertEqual(self.axe(p), [], scheme)

    # --- Review: one card at a time --------------------------------------------------------------------
    def deck(self, *titles):
        APP["dash"].update(review=[suggestion(i + 1, t) for i, t in enumerate(titles)], review_count=len(titles))
        self.addCleanup(APP["dash"].update, review=[], review_count=0)
        p = self.page()
        p.click('[data-tab="review"]')
        p.wait_for_selector("#deckcard")
        del POSTS[:]
        return p

    def test_the_deck_shows_one_and_the_keys_decide(self):
        p = self.deck("first", "second")
        self.assertEqual(p.locator("#deckcard").count(), 1)
        self.assertIn("1 of 2", p.inner_text(".deckhead"))
        p.keyboard.press("ArrowRight")
        p.wait_for_timeout(300)
        self.assertIn(("/api/app/entry", '{"id":1,"review":"accepted"}'), POSTS)
        p.keyboard.press("ArrowLeft")
        p.wait_for_timeout(300)
        self.assertIn(("/api/app/entry", '{"id":1,"review":"rejected","status":"dismissed"}'), POSTS)

    def test_dragging_the_card_decides_and_a_small_drag_doesnt(self):
        p = self.deck("first")
        b = p.locator("#deckcard").bounding_box()
        x, y = b["x"] + b["width"] / 2, b["y"] + 60
        p.mouse.move(x, y)
        p.mouse.down()
        p.mouse.move(x + 40, y)
        p.mouse.up()
        p.wait_for_timeout(300)
        self.assertEqual(POSTS, [], "too short: the card springs back")
        p.mouse.move(x, y)
        p.mouse.down()
        p.mouse.move(x + b["width"] * .3, y)
        p.mouse.move(x + b["width"] * .45, y)
        p.mouse.up()
        p.wait_for_timeout(300)
        self.assertIn(("/api/app/entry", '{"id":1,"review":"accepted"}'), POSTS)

    def test_undo_puts_the_last_one_back(self):
        p = self.deck("first", "second")
        p.click("#deckdrop")
        p.wait_for_selector("#deckundo")
        p.keyboard.press("z")
        p.wait_for_timeout(200)
        self.assertIn(("/api/app/entry", '{"id":1,"review":"unreviewed","status":"open"}'), POSTS)

    def test_what_passed_its_date_waits_apart(self):
        APP["dash"].update(review_expired=[suggestion(9, "gone")], review_expired_count=1)
        self.addCleanup(APP["dash"].update, review_expired=[], review_expired_count=0)
        p = self.page()
        p.click('[data-tab="review"]')
        self.assertIn("passed its date", p.inner_text(".deckdone"))
        p.click("#deckexpired")
        self.assertIn("Gone", p.inner_text("#deckcard"))
        self.assertIn("Passed their date", p.inner_text(".deckhead"))

    def test_done_button_and_key(self):
        p = self.page()
        self.mentions(p)
        self.assertEqual(p.locator("[data-cdone]").count(), 1, "one verb for needs you and follow up")
        p.click('.trow[data-sel="k-follow"]')
        del POSTS[:]
        p.focus(".trow.sel")
        p.keyboard.press("d")
        p.wait_for_function("document.querySelector('#toast').textContent.startsWith('Done')")
        self.assertIn(("/api/app/resolve", '{"card":"k-follow"}'), POSTS)

    # --- what axe doesn't check -----------------------------------------------------------------------
    def test_forced_colors_keep_state_and_focus(self):
        p = self.page(forced="active")
        self.mentions(p)
        style = lambda q, prop: p.evaluate("([q, prop]) => getComputedStyle(document.querySelector(q))[prop]", [q, prop])
        self.assertEqual(style('.trow[data-sel="k-needs"]', "borderLeftStyle"), "double", "Needs you rows keep a marker")
        self.assertNotEqual(style('.trow[data-sel="k-answered"]', "borderLeftStyle"), "double")
        self.assertNotEqual(style(".trow.sel", "outlineStyle"), "none", "the selected row is outlined")
        p.focus(".trow.sel")
        p.keyboard.press("j")
        self.assertNotEqual(style(".trow:focus-visible", "outlineStyle"), "none", "the focused row is outlined")

    def test_native_controls_follow_theme(self):
        for scheme in ("light", "dark"):
            p = self.page(scheme=scheme)
            self.assertEqual(p.evaluate("getComputedStyle(document.documentElement).colorScheme"), scheme, "scrollbars and pickers")
            self.assertNotEqual(p.evaluate("getComputedStyle(document.documentElement).scrollbarColor"), "auto")

    def test_reflow_320(self):
        p = self.page(width=320)
        for t in ("home", "review", "mentions", "notes", "settings"):
            p.click('[data-tab="%s"]' % t)
            self.assertLessEqual(p.evaluate("document.documentElement.scrollWidth"), 320, t + " scrolls sideways at 320px")
            if t != "settings":
                cut = p.evaluate("[...document.querySelectorAll('.trow .who')].filter((e) => e.scrollWidth > e.clientWidth + 1).length")
                self.assertEqual(cut, 0, t + ": short names aren't cut off")

    def test_axe(self):
        for width in (1360, 430):
            for scheme in ("light", "dark"):
                p = self.page(width, scheme)
                try:
                    p.add_script_tag(url=AXE)
                except Exception as e:  # pragma: no cover
                    self.skipTest("axe-core unreachable: %s" % e)
                for t in ("home", "review", "mentions", "notes", "settings"):
                    p.click('[data-tab="%s"]' % t)
                    self.assertEqual(self.axe(p), [], "%s %dpx %s" % (t, width, scheme))
                p.click('[data-tab="mentions"]')
                p.focus("[data-lb]")
                p.keyboard.press("Enter")
                self.assertEqual(self.axe(p), [], "viewer %dpx %s" % (width, scheme))

    def axe(self, p):
        return p.evaluate("async () => (await axe.run(document)).violations.map((v) => v.id + ': ' + v.nodes.map((n) => n.target.join(' ')).join(', '))")

    # --- what the org chats will look like (made-up) --------------------------------------------------
    def org_data(self):
        """ECG/IMES-style traffic: long chat and people names, announcements, and far more handled than open."""
        long_chat = "[IMES] Committee on Publicity, Documentation and Creatives — Batch 2026–2027 (Official)"
        announce = ("📢 ANNOUNCEMENT: Good evening, everyone! Please be reminded that the General Assembly will be held on "
                    "Friday at the Engineering Lobby, 5:00 PM onwards. Attendance is mandatory for all officers and "
                    "committee heads. Kindly react once seen. https://forms.example.invalid/ga-attendance")
        cs = []
        for i in range(130):
            state = "needs" if i < 5 else "followup" if i < 10 else "answered" if i < 70 else "fyi"
            asker = "Maria Concepcion Dela Cruz-Villanueva" if i % 3 == 0 else "Jo"
            c = card("o%d" % i, state, asker, "fyi" if state == "fyi" else "request",
                     handled=None if state == "needs" else {"how": "you replied", "ts": ts(20)},
                     asks=(announce,) if state == "fyi" else ("DCCO pa-check po ng pubmat", "Creatives, sino available bukas?"),
                     why="they're waiting on you" if state == "followup" else None)
            c.update(chat_name=long_chat if i % 2 else "[ECG] EB", org="imes" if i % 2 else "ecg", last_ping=ts(30 + i))
            cs.append(c)
        old = APP["dash"]["cards"]
        APP["dash"]["cards"] = cs
        self.addCleanup(APP["dash"].__setitem__, "cards", old)
        return cs

    def test_org_scale_caps_handled_groups(self):
        self.org_data()
        p = self.page()
        self.mentions(p)
        counts = [" ".join(t.split()) for t in p.locator(".grp").all_inner_texts()]
        self.assertEqual(counts, ["NEEDS YOU 5", "FOLLOW UP 5", "ANSWERED 60", "FOR YOUR INFORMATION 60"], "group counts are totals")
        self.assertEqual(p.locator(".trow").count(), 5 + 5 + 40 + 40)
        self.assertEqual(p.locator(".tlist button").count(), 0, "Show all sits outside the listbox")
        p.focus(".trow.sel")
        p.keyboard.press("End")
        self.assertEqual(p.get_attribute(".trow.sel", "data-sel"), "o109", "End stops at the newest 40 FYI")
        p.click("[data-showall]")
        self.assertEqual(p.locator(".trow").count(), 130)
        self.assertEqual(p.locator("[data-showall]").count(), 0)
        self.assertTrue(self.active(p, "a.dataset.sel === 'o50'"), "focus lands on the first row it revealed")

    def test_org_style_data_fits(self):
        self.org_data()
        for width in (1360, 430, 320):
            p = self.page(width)
            for t in ("home", "mentions"):
                p.click('[data-tab="%s"]' % t)
                self.assertLessEqual(p.evaluate("document.documentElement.scrollWidth"), width, "%s %dpx scrolls sideways" % (t, width))
            over = p.evaluate("[...document.querySelectorAll('.trow')].filter((r) => r.scrollWidth > r.clientWidth + 1).length")
            self.assertEqual(over, 0, "%dpx: long chat names stay inside their rows" % width)
            if width == 1360:
                p.click('.trow[data-sel="o80"]')  # an announcement
                box = p.evaluate("(() => { const d = document.querySelector('.tdetail'); return [d.scrollWidth, d.clientWidth]; })()")
                self.assertLessEqual(box[0], box[1] + 1, "a long announcement and its link wrap")

    def test_org_style_axe(self):
        self.org_data()
        for width in (1360, 430):
            p = self.page(width, "dark")
            try:
                p.add_script_tag(url=AXE)
            except Exception as e:  # pragma: no cover
                self.skipTest("axe-core unreachable: %s" % e)
            for t in ("home", "mentions"):
                p.click('[data-tab="%s"]' % t)
                self.assertEqual(self.axe(p), [], "org data, %s %dpx" % (t, width))


if __name__ == "__main__":
    unittest.main()
