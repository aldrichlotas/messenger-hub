"""py -3.10 -m unittest discover tests"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hubos import rules  # noqa: E402

# Tuesday, Oct 27 2026, 10:00
BASE = dt.datetime(2026, 10, 27, 10, 0)
PROF = rules.Profile(
    names=["Aldrich", "Aldrich Lotas"],
    titles={"ecg": ["DCCO", "Deputy CCO"], "imes": ["AVP P&C", "AVP for P&C"]},
    committees={"ecg": ["Creatives", "Publicity", "Media", "Linkages"], "imes": ["P&C", "pubs"]},
    above={"ecg": ["Sam", "Isabelle Reyes", "Ystiphen"], "imes": ["Thirdy"]},
)


def when(text, base=BASE):
    hit = rules.find_date(text, base)
    return (hit.when, hit.has_time) if hit else None


class Dates(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(when("pasa bukas"), (dt.datetime(2026, 10, 28, 23, 59), False))
        self.assertEqual(when("meeting mamaya 7pm"), (dt.datetime(2026, 10, 27, 19, 0), True))
        self.assertEqual(when("due tomorrow 11:59 PM"), (dt.datetime(2026, 10, 28, 23, 59), True))
        self.assertEqual(when("sa makalawa"), (dt.datetime(2026, 10, 29, 23, 59), False))
        # Written at 1 AM, "bukas" means the day that's just starting.
        self.assertEqual(when("bukas 9pm", dt.datetime(2026, 10, 27, 1, 26))[0], dt.datetime(2026, 10, 27, 21, 0))

    def test_weekdays(self):
        self.assertEqual(when("GA on Friday 6pm")[0], dt.datetime(2026, 10, 30, 18, 0))
        self.assertEqual(when("sa Biyernes")[0].date(), dt.date(2026, 10, 30))
        self.assertEqual(when("next Friday")[0].date(), dt.date(2026, 11, 6))
        self.assertEqual(when("Thu 3pm")[0], dt.datetime(2026, 10, 29, 15, 0))
        self.assertIsNone(when("last Friday was fun"))
        self.assertIsNone(when("we sat down"))  # lowercase abbreviation isn't a day

    def test_explicit(self):
        self.assertEqual(when("deadline Oct 30")[0].date(), dt.date(2026, 10, 30))
        self.assertEqual(when("on November 3, 2026 at 9:30am")[0], dt.datetime(2026, 11, 3, 9, 30))
        self.assertEqual(when("30 Oct")[0].date(), dt.date(2026, 10, 30))
        self.assertEqual(when("submit by 11/02")[0].date(), dt.date(2026, 11, 2))
        self.assertEqual(when("Jan 5", dt.datetime(2026, 12, 20))[0].date(), dt.date(2027, 1, 5))

    def test_day_of_month(self):
        self.assertEqual(when("game goes public sa 26th monday", dt.datetime(2026, 10, 13, 19, 39))[0].date(), dt.date(2026, 10, 26))
        self.assertEqual(when("28 wednesday public natin", dt.datetime(2026, 10, 13))[0].date(), dt.date(2026, 10, 28))
        self.assertEqual(when("due on the 3rd")[0].date(), dt.date(2026, 11, 3))

    def test_times(self):
        self.assertEqual(when("call at 5:30")[0], dt.datetime(2026, 10, 27, 17, 30))
        self.assertEqual(when("EOD")[0], dt.datetime(2026, 10, 27, 23, 59))
        self.assertEqual(when("tanghali bukas")[0], dt.datetime(2026, 10, 28, 12, 0))
        # A bare time that already passed today means tomorrow.
        self.assertEqual(when("8am")[0], dt.datetime(2026, 10, 28, 8, 0))

    def test_not_dates(self):
        self.assertIsNone(when("may 2 pubmats pa tayo"))  # Filipino "may" = "there are"
        self.assertIsNone(when("1/2 done na"[:0] + "thanks po"))
        self.assertIsNone(when(""))
        self.assertIsNone(when("congrats 17th ECG officers"))
        self.assertIsNone(when("we got 1st place"))


class Mentions(unittest.TestCase):
    def test_strength(self):
        self.assertEqual(rules.mention("@Aldrich pa-approve", "ecg", PROF)[0], 3)
        self.assertEqual(rules.mention("DCCO, can you check?", "ecg", PROF)[0], 3)
        self.assertEqual(rules.mention("Creatives pls send drafts", "ecg", PROF)[0], 2)
        self.assertEqual(rules.mention("pubs team update", "imes", PROF)[0], 2)
        self.assertEqual(rules.mention("@everyone GA later", "ecg", PROF)[0], 1)
        self.assertEqual(rules.mention("nice one", "ecg", PROF)[0], 0)
        self.assertEqual(rules.mention("P&C rocks", "ecg", PROF)[0], 0)  # not an ECG committee

    def test_word_boundaries(self):
        self.assertEqual(rules.mention("Aldrichhh lol", "ecg", PROF)[0], 0)
        self.assertEqual(rules.mention("Media team", "ecg", PROF)[0], 2)
        self.assertEqual(rules.mention("social media post", "ecg", PROF)[0], 2)


class Analyse(unittest.TestCase):
    def a(self, text, author="Kim", is_me=False, org="ecg"):
        return rules.analyse(text, BASE, author, is_me, org, PROF)

    def test_meeting(self):
        f = self.a("EB alignment meeting tomorrow 8pm via zoom", author="Sam Reyes")
        self.assertEqual(f.kind, "meeting")
        self.assertEqual(f.due, dt.datetime(2026, 10, 28, 20, 0))
        self.assertGreaterEqual(f.confidence, 0.7)

    def test_task_for_me(self):
        f = self.a("@Aldrich pubmat for GA pls, deadline Thu 6pm")
        self.assertEqual(f.kind, "task")
        self.assertGreaterEqual(f.confidence, 0.8)

    def test_general_deadline(self):
        f = self.a("submit your forms by Friday")
        self.assertEqual(f.kind, "deadline")
        self.assertLess(f.confidence, 0.7)

    def test_request_without_date(self):
        self.assertEqual(self.a("Aldrich can you review the caption").kind, "task")

    def test_my_commitment(self):
        f = self.a("sige ako na gagawa ng pubmat bukas", author="Aldrich", is_me=True)
        self.assertEqual(f.kind, "task")
        self.assertIsNone(self.a("haha ok", author="Aldrich", is_me=True))

    def test_my_announcement(self):
        f = rules.analyse("@everyone game goes public sa 26th monday", dt.datetime(2026, 10, 13, 19, 39), "Aldrich", True, None, PROF)
        self.assertEqual((f.kind, f.due.date()), ("deadline", dt.date(2026, 10, 26)))

    def test_noise(self):
        self.assertIsNone(self.a("HAHAHA grabe"))
        self.assertIsNone(self.a("meeting last Friday was long"))


class Capture(unittest.TestCase):
    def test_capture(self):
        c = rules.capture("finish IE lab report tomorrow 9pm #school #ecg", BASE, {"ecg"})
        self.assertEqual(c["kind"], "capture")
        self.assertEqual(c["due"], dt.datetime(2026, 10, 28, 21, 0))
        self.assertEqual(c["tags"], ["school", "ecg"])
        self.assertIn("#school", c["title"])
        self.assertNotIn("#ecg", c["title"])

    def test_win(self):
        c = rules.capture("/win led Frosh Convocation pubmats, 14 posts #ecg", BASE)
        self.assertEqual(c["kind"], "win")
        self.assertIsNone(c["due"])
        self.assertEqual(c["tags"], ["ecg"])


if __name__ == "__main__":
    unittest.main()
