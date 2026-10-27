"""Export clean-up: rows a scrape captured twice."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "messenger"))
import common  # noqa: E402


def msg(sender, text, ts="2026-10-27T22:57", **kw):
    return {"kind": "message", "sender": sender, "text": text, "ts": ts, "attachments": [], **kw}


class DropRepeatsTests(unittest.TestCase):
    def test_rebuilt_block_is_dropped(self):
        a, b = msg("Aldrich", "wyd"), msg("Patrick", "chillin")
        rows = [a, b, dict(a), dict(b), msg("Aldrich", "Ouuu", "2026-10-27T23:01")]
        self.assertEqual([m["text"] for m in common.drop_repeats(rows)], ["wyd", "chillin", "Ouuu"])

    def test_back_to_back_identical_messages_are_real(self):
        rows = [msg("Patrick", "lmfaoo"), msg("Patrick", "lmfaoo")]
        self.assertEqual(len(common.drop_repeats(rows)), 2)

    def test_same_text_another_minute_is_kept(self):
        rows = [msg("Kim", "G"), msg("Aldrich", "ok"), msg("Kim", "G", "2026-10-27T22:58")]
        self.assertEqual(len(common.drop_repeats(rows)), 3)

    def test_far_apart_is_kept(self):
        rows = [msg("Kim", "G")] + [msg("P", f"m{i}") for i in range(45)] + [msg("Kim", "G")]
        self.assertEqual(len(common.drop_repeats(rows)), 47)


if __name__ == "__main__":
    unittest.main()
