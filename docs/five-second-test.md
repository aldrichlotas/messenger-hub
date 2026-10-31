# Five-second test: Now

Checks that someone who has never seen the hub can tell, at a glance, what is waiting on you and whether it's current.
Takes about 5 minutes with one org-mate. Nothing leaves your laptop; they look at your screen.

## Setup

1. Open the app (`py -3.10 hub.py app`) on **Now**, and make sure at least one row is under **Do now**
   (if none is, run it after the next sync, or pick a day when something is open). To show someone without
   showing them your chats, use `py -3.10 demo/run_demo.py` instead: everything in it is made up.
2. Note on paper, without showing them, the answers you expect for the questions below.
3. Cover the screen (or switch to another tab).

## Run it

1. "I'll show you a screen for five seconds. Just look." Uncover Now, count five seconds, cover it.
2. Ask, and write down what they say, word for word:
   1. How many things are waiting on me?
   2. What should I do first, and who is it for?
   3. Is this up to date?
   4. Which of these did I already deal with?
3. Uncover it. "Open the first one, and tell me what it's about." Time them from the moment they
   touch the mouse or keyboard to their answer.
4. "Now mark it done." Note whether they find the button or the `d` key without help.

## Score

| Question | Pass when |
|---|---|
| 1. How many | the exact number under Do now |
| 2. First | the lead item and the right person, in their own words |
| 3. Up to date | they read the status at the top correctly |
| 4. Dealt with | they point to Dealt with |
| 3 (timed) | under 15 seconds, without asking what anything means |
| 4 (mark done) | done without help |

Four of six passes is fine; anything they got wrong or asked about is the thing to fix. Write the
answers and the date in `data/reports/five-second-test.md` (git-ignored) so the next run can be compared.

## Numbers from real use

`py -3.10 hub.py threads --report` also shows, from the app's event log, how many times you opened
Mentions with something waiting (`mentions_opened_7d`), how often you got it to zero (`cleared_7d`),
the median seconds that took (`median_seconds_to_clear`), and how many times you jumped to Messenger
(`opened_in_messenger_7d`). If clearing gets slower or jumps to Messenger climb, the rows aren't saying
enough on their own.
