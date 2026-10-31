"""Conversation split and mention cards (no network, no tokens)."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import process, rules, threads  # noqa: E402
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

    def test_asks_keep_message_boundaries(self):
        cs = self.cards((1, 0, "Patrick", "give me skill ideas"), (2, 1, "Patrick", "ermm"),
                        (3, 1, "Patrick", "how would that work with our physics"), (4, 2, "Patrick", "@Aldrich naw"), (5, 2, "Patrick", "@Aldrich?"))
        self.assertEqual(cs[0]["asks"], ["give me skill ideas", "ermm", "how would that work with our physics", "naw"])
        self.assertIn("ermm how would", cs[0]["gist"])  # the one-line gist is unchanged

    def test_reply_that_puts_it_off_is_a_followup(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich can we use ur spare email for tiktok"), (2, 1, "Aldrich", "G"),
                        (3, 2, "Patrick", "send it fr"), (4, 3, "Aldrich", "Hol up"))
        self.assertEqual((cs[0]["state"], cs[0]["why"]), ("followup", "you said you'd get to it"))
        self.assertIsNotNone(cs[0]["handled"])  # still counts as replied: no unread badge

    def test_they_wait_on_you_is_a_followup(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich give me skill ideas"), (2, 1, "Aldrich", "rocket boosters"),
                        (3, 5, "Patrick", "actually imma just wait until ur available to call tom"))
        self.assertEqual((cs[0]["state"], cs[0]["why"]), ("followup", "they're waiting on you"))

    def test_new_ask_after_your_reply_needs_you(self):
        cs = self.cards((1, 0, "Gab", "@Aldrich when y'all releasing the game"), (2, 1, "Aldrich", "Next week Monday"),
                        (3, 2, "Gab", "Does Enzo know about the game"))
        self.assertEqual((cs[0]["state"], cs[0]["why"]), ("needs", "asked again after your reply"))
        self.assertIsNone(cs[0]["handled"])

    def test_done_followup_stays_done_until_something_new(self):
        msgs = [(1, 0, "Patrick", "@Aldrich can we use ur spare email for tiktok"), (2, 1, "Aldrich", "G"),
                (3, 2, "Patrick", "send it fr"), (4, 3, "Aldrich", "Hol up")]
        c = self.cards(*msgs)[0]
        self.assertEqual(c["state"], "followup")
        threads.mark_done(self.st, c["key"], c["end"])
        c = self.cards(*msgs)[0]
        self.assertEqual((c["state"], c["why"], c["handled"]["how"]), ("answered", None, "you marked it done"))
        c = self.cards(*msgs, (5, 10, "Patrick", "thanks bro"))[0]
        self.assertEqual(c["state"], "answered", "chat after Done doesn't undo it; the old 'hol up' is spent")
        c = self.cards(*msgs, (5, 10, "Patrick", "thanks bro"), (6, 11, "Aldrich", "teka one more thing"))[0]
        self.assertEqual((c["state"], c["why"]), ("followup", "you said you'd get to it"), "a new put-off brings it back")
        threads.mark_done(self.st, c["key"], None)  # Not done yet
        self.assertEqual(threads.cards(self.st, process.channels(CFG), process.profile(CFG))[0]["state"], "followup")

    def test_old_database_gets_done_column(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE conv_state (key TEXT PRIMARY KEY, read_upto TEXT, summary TEXT, summary_hash TEXT)")
        db.commit()
        db.close()
        st = Store(path)
        threads.mark_done(st, "eb#1", "2026-10-28T10:00")
        self.assertEqual(st.one("SELECT done_upto FROM conv_state")["done_upto"], "2026-10-28T10:00")
        st.db.close()

    def test_plain_reply_stays_answered(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich are you free"), (2, 1, "Aldrich", "yes"), (3, 2, "Kim", "bet"))
        self.assertEqual((cs[0]["state"], cs[0]["why"]), ("answered", None))

    def test_answered_review_keeps_verdicts_and_scores(self):
        cs = self.cards((1, 0, "Patrick", "@Aldrich can you check the build"), (2, 3, "Aldrich", "brb"),
                        (3, 4, "Patrick", "ok call me tom then"),
                        (4, 300, "Kim", "@Aldrich are you free"), (5, 302, "Aldrich", "yes"))
        with mock.patch.object(threads, "REPORTS", self.tmp.name):
            path, n, done = threads.answered_review(cs)
            self.assertEqual((n, done), (2, 0))
            import csv
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            pat = next(r for r in rows if r["asker"] == "Patrick")
            self.assertEqual(pat["your_reply"], "brb")
            self.assertEqual(pat["after_your_reply"], "Patrick: ok call me tom then")
            for r in rows:  # what you'd type in Excel: a follow-up you owe, and one that's done
                r["verdict"] = "f" if r["asker"] == "Patrick" else "Done"
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, threads.REVIEW_COLS)
                w.writeheader()
                w.writerows(rows)
            self.assertEqual(threads.answered_review(cs)[2], 2, "re-exporting keeps your verdicts")
            s = threads.answered_score(cs)
        self.assertEqual(s["precision_as_labelled"], "1/2 = 50%")
        self.assertEqual(s["misses"], {"followup": 1})
        self.assertEqual(s["misses_now_shown"], 1)  # the "brb" reply is now a follow-up, not hidden
        self.assertEqual(s["precision_now"], "1/1 = 100%")

    def test_org_mention_sample_and_score(self):
        cs = self.cards((1, 0, "Sam", "@Aldrich pa-check ng pubmat"),
                        (2, 200, "Kim", "si aldrichh daw yung gagawa"),  # misspelt: the rules miss it, the sample doesn't
                        (3, 400, "Kim", "Creatives meeting later"),
                        (4, 600, "Kim", "unrelated"))
        with mock.patch.object(threads, "REPORTS", self.tmp.name):
            path, n, total = threads.mention_sample(self.st, cs, process.profile(CFG), "ecg")
            self.assertEqual((n, total), (3, 3), "only messages naming you, title or committee, any case")
            import csv
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(next(r for r in rows if "aldrichh" in r["text"])["rules"], "no card")
            for r in rows:
                r["for_me"] = "n" if "Creatives" in r["text"] else "y"
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, threads.SAMPLE_COLS)
                w.writeheader()
                w.writerows(rows)
            threads.mention_sample(self.st, cs, process.profile(CFG), "ecg")  # re-export keeps the labels
            s = threads.mention_score(cs, "ecg")
            self.assertEqual(threads.answered_review(cs, "ecg")[0], os.path.join(self.tmp.name, "answered-review-ecg.csv"))
        self.assertEqual(s["labelled"], 3)
        self.assertEqual(len(s["missed_no_card"]), 1)
        self.assertEqual(s["for_me_by_state"]["needs"], 1)

    def test_org_flood_rules(self):
        cs = self.cards((1, 0, "Kim", "@everyone upload your photos by tonight 11:59PM"),  # broadcast with a deadline: no card
                        (2, 200, "Sam", "@Aldrich kindly note, PubPro has 0 applicants"),     # an ask without a question mark
                        (3, 400, "Sam", "@Aldrich kindly change the password"), (4, 401, "Aldrich", "thank you!"))
        self.assertFalse([c for c in cs if c["asker"] == "Kim"], "@everyone alone isn't a card")
        note = next(c for c in cs if "PubPro" in c["gist"])
        self.assertEqual((note["label"], note["state"]), ("request", "needs"))
        pw = next(c for c in cs if "password" in c["gist"])
        self.assertEqual((pw["state"], pw["why"]), ("followup", "you only said thanks"))

    def test_thanks_then_ask_is_about_the_ask(self):
        cs = self.cards((1, 0, "Kim", "oh ok noted, thank you so much Aldrich"), (2, 4, "Sam", "hi @Aldrich"), (3, 4, "Sam", "please check tg"))
        self.assertEqual((cs[0]["label"], cs[0]["state"]), ("request", "needs"))
        self.assertIn("please check tg", cs[0]["gist"])

    def test_finished_closes_a_promise(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich can you shrink the bingo cards"), (2, 3, "Aldrich", "I'll do it right now"),
                        (3, 20, "Aldrich", "I've updated the bingo cards na"))
        self.assertEqual((cs[0]["state"], cs[0]["why"]), ("answered", None))

    def test_promise_without_finishing_stays_followup(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich can you shrink the bingo cards"), (2, 3, "Aldrich", "I'll do it right now"))
        self.assertEqual(cs[0]["state"], "followup")

    def test_teammate_took_the_committee_ask(self):
        cs = self.cards((1, 0, "Kim", "Creatives: please fix the main pub"), (2, 5, "Gio", "Will be working on this tonight"))
        self.assertEqual(cs[0]["state"], "answered")
        self.assertEqual(cs[0]["handled"]["how"], "taken by Gio")

    def test_teammate_saying_so_doesnt_take_a_named_ask(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich please fix the main pub"), (2, 5, "Gio", "Will be working on this tonight"))
        self.assertEqual(cs[0]["state"], "needs")  # asked of you by name: it stays yours

    def test_newer_reply_settles_the_older_ask(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich I will need your help with the primer and bingo card"),
                        (2, 300, "Kim", "@Aldrich here is the primer content and bingo card, please check"), (3, 303, "Aldrich", "sent the file already"))
        old = next(c for c in cs if "need your help" in c["gist"])
        self.assertEqual((old["state"], old["handled"]["how"]), ("answered", "you replied later"))

    def test_shared_names_arent_a_topic(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich Patrick Reyes Lim Cruz monitor the slides"), (2, 1, "Patrick Reyes Lim Cruz", "ok"),
                        (3, 300, "Kim", "@Aldrich Patrick Reyes Lim Cruz sign the contract"), (4, 303, "Aldrich", "sent the file already"))
        self.assertEqual(next(c for c in cs if "slides" in c["gist"])["state"], "needs")

    def test_newer_reply_on_another_topic_settles_nothing(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich I will need your help with the primer and bingo card"),
                        (2, 300, "Kim", "@Aldrich can you sign the venue contract"), (3, 303, "Aldrich", "sent the file already"))
        self.assertEqual(next(c for c in cs if "primer" in c["gist"])["state"], "needs")

    def test_newer_reply_to_someone_else_settles_nothing(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich I will need your help with the primer and bingo card"),
                        (2, 300, "Gio", "@Aldrich here is the primer content and bingo card, please check"), (3, 303, "Aldrich", "sent the file already"))
        self.assertEqual(next(c for c in cs if c["asker"] == "Kim")["state"], "needs")

    def test_namesake_stays_whole_in_the_gist(self):
        prof = rules.Profile(names=["Aldrich"], not_me=["Aldrich Sy"])
        self.assertEqual(threads.strip_names("boss @Aldrich Sy and @Aldrich pasign", prof), "boss Aldrich Sy and pasign")

    def test_all_your_names_are_you(self):
        cfg = dict(CFG, profile={"names": ["Aldrich", "Aldrich Lotas"]})
        self.assertTrue(process.is_mine("Aldrich Lotas", cfg))
        self.assertTrue(process.is_mine("aldrich", cfg))
        self.assertFalse(process.is_mine("Aldrich Cruz", cfg))

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

    def photo(self, name, data=b"jpg"):
        os.makedirs(os.path.join(self.tmp.name, "eb", "attachments"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "eb", "attachments", name), "wb") as f:
            f.write(data)
        return {"attachments": [{"type": "image", "path": f"attachments/{name}"}]}

    def test_ask_with_a_photo_is_review(self):
        cs = self.cards((1, 0, "Gab", "@Aldrich goods na ba?"), (2, 0, "Gab", "", self.photo("pubmat 1.jpg")))
        self.assertEqual(cs[0]["label"], "review")
        self.assertEqual(cs[0]["state"], "needs")
        self.assertEqual(cs[0]["media"], [{"type": "image", "url": "/media/eb/attachments/pubmat%201.jpg"}])
        # The caption and its photo read as one message.
        lines = [ln for ln in cs[0]["excerpt"] if "gap" not in ln]
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]["media"]), 1)

    def test_design_link_is_review(self):
        cs = self.cards((1, 0, "Kim", "@Aldrich pacheck https://www.canva.com/design/abc/edit", {"links": ["https://www.canva.com/design/abc/edit"]}))
        self.assertEqual(cs[0]["label"], "review")
        self.assertEqual(cs[0]["links"][0]["site"], "Canva design")

    def test_fyi_you_replied_to_stays_fyi(self):
        cs = self.cards((1, 0, "Gab", "using @Aldrich laptop"), (2, 2, "Aldrich", "lol ok"))
        self.assertEqual(cs[0]["state"], "fyi")

    def test_media_check_finds_missing_files(self):
        self.cards((1, 0, "Gab", "@Aldrich", self.photo("ok.jpg")),
                   (2, 1, "Gab", "", {"attachments": [{"type": "image", "path": "attachments/gone.jpg"}]}))
        r = threads.media_check(self.st, process.channels(CFG))
        self.assertEqual((r["in_export"], r["broken"], r["missing"]), (2, 1, ["eb/attachments/gone.jpg"]))


class MediaServeTests(unittest.TestCase):
    """/media/ serves only attachments of chats you added, and never outside the chat's folder."""

    def setUp(self):
        import http.server
        import threading

        from hubos import app

        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "eb", "attachments"))
        with open(os.path.join(self.tmp.name, "eb", "attachments", "a.jpg"), "wb") as f:
            f.write(b"0123456789")
        with open(os.path.join(self.tmp.name, "eb", "messages.jsonl"), "w") as f:
            f.write("")
        self.patches = [mock.patch.object(process, "CHATS", self.tmp.name), mock.patch.object(app.hub, "load_config", return_value=CFG)]
        for p in self.patches:
            p.start()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        import http.client

        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_serves_and_seeks(self):
        self.assertEqual(self.get("/media/eb/attachments/a.jpg"), (200, b"0123456789"))
        self.assertEqual(self.get("/media/eb/attachments/a.jpg", {"Range": "bytes=2-4"}), (206, b"234"))
        self.assertEqual(self.get("/media/eb/attachments/a.jpg", {"Range": "bytes=50-"})[0], 416)

    def test_refuses_outside(self):
        for path in ("/media/eb/messages.jsonl", "/media/eb/attachments/../messages.jsonl",
                     "/media/eb/attachments/%2e%2e/messages.jsonl", "/media/other/attachments/a.jpg", "/media/eb/attachments/nope.jpg"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
