# Messenger Hub

Your personal operating system for school, orgs and projects: a desktop app with what's due today,
your deadlines and every place someone mentioned you, a Telegram bot for briefs, reminders and
quick notes, and per-org reports written by Claude. The design is in [docs/DESIGN.md](docs/DESIGN.md).

## The app

```bash
py -3.10 hub.py app            # opens the app (and starts the hub if it isn't running)
```

- **Home**: Today (overdue, due today, undated notes), Deadlines (tomorrow, next 7 days, later, with
  countdowns), and what in Mentions still needs you.
- **Mentions**, so you don't have to backread: one row per conversation that pings you (your name, titles,
  committees, or a reply to your message), grouped into Needs you / Answered / For your information. A row
  shows who, what kind of ask it is (asks you, wants you to review this, FYI…), the ask in one line, how
  it was handled (you replied within an hour, or someone else answered), its age and up to 3 photos.
  Selecting it shows the whole conversation on the right with photos and videos (click for full size) and
  links as chips naming the site; in a narrow window it opens under its row. An ask that comes with a
  photo or a Canva/Figma/Drive link is labelled **wants you to review this**. Keys: `j`/`k` move, `Enter`
  open, `e` mark read, `o` open in Messenger. Optional: Settings → One-line summaries → Lite has Haiku
  write a line per open conversation (uses your Claude plan).
  `py -3.10 hub.py threads --report` checks the conversation split, that every saved attachment is on
  disk, and how long clearing Mentions takes (from the app's event log); it writes sample cards to
  `data/reports/mention-cards.md` for someone else to read cold. [docs/five-second-test.md](docs/five-second-test.md)
  is a short test to run with an org-mate.
- **Review**: dates, meetings and requests the rules found in your chats. Accept what's yours.
- **Wins**: your achievements log, for LinkedIn later.
- **Settings**: your names, your notes-to-self chat, your title, committees and the people above
  you in each org; the Telegram bot; brief times, quiet hours and the alert cap; start with Windows;
  a desktop shortcut.
- **Floating bar** (Float, top right): a slim strip on top of your other windows with the sync status and your next thing. Done works from it, and it opens to the rest of Do now. Top-right arrow goes back to the full hub. 
  Closing the window keeps the hub running for briefs; "Quit hub" in Settings stops it.

All of this is **local and uses no Claude tokens**: rules in [hubos/rules.py](hubos/rules.py) find
dates ("bukas 7pm", "sa Biyernes", "Oct 30"), mentions and requests ("pubmat", "pa-approve").
Claude is only used by `analyze` / `/digest` below.

**Live sync** (Settings, off by default): while the hub runs, new messages reach their cards without
pressing Sync. The hub's browser stays open, minimised, and reads your chat list every few seconds
without opening a chat. It opens a chat when its row changes, and looks through every chat once every
3 hours in case the list missed one. Community chats may only be caught by that slow pass. Nothing
opens past the daily limit (300 chats); quiet hours only silence Telegram. A login, PIN or checkpoint page pauses it
until you press Resume, and it tells you in the app and on Telegram. While it's on, use the app's Sync
button rather than `hub.py sync`, which can't run while live sync holds the browser. `py -3.10 hub.py live`
reports the delay, chats opened per day and any checkpoints; `hub.py live --audit` compares the last
24 hours against a separate full read.

**Refreshing your chat list** (config page): reads your Messenger chat list and stops once it reaches
chats quiet for over a year. Tick *Find all chats* (or `hub.py discover --all-chats`) to go on to the
very end. *Stop* ends a refresh early and keeps what it found. A refresh only ever adds: chats found earlier stay on the list. It can't run while live
sync is on, because both need the same browser.

**How Sync stays quick:** it reads your chat list first and opens only chats whose row changed. Every
chat still gets opened at least once a day, in case the list didn't show a change. Press *Sync* again while it runs (it reads *Stop sync*) to stop: a chat being read for the first time keeps what it has, a chat already synced is read again next time, and chats not reached wait for the next sync. The same goes for a chat that runs out of time, so a sync never leaves a gap in its history. Your notes-to-self chat's first sync reads back 30 days, not to the start of the chat. *Full sync* in
Settings (or `hub.py sync --all --every-chat`) opens every chat right away.

**Capturing things:** type in the box at the top of the app, send any text to the Telegram bot, or
message yourself on Messenger as you already do (pick that chat in Settings; it syncs with the
rest). A date in the note sets its due time; `#ecg` files it under an org; `/win …` or `win: …` logs
an achievement.

**Telegram bot:** create one with @BotFather, paste its token in Settings, then send it
`/start <code>` (the code is shown in Settings). Or put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID`
(your numeric id, e.g. from @userinfobot) in `.env` and press Start in the bot's chat once; no code
needed. Briefs arrive at the times you set (default 07:00,
13:00, 21:00), alerts for meetings in 30 min and things due within 2 h / 24 h, capped per day and
silent in quiet hours. The bot only shows org, type, time and a cleaned title; message text stays on
the laptop. Commands: `/today`, `/due`, `/done 12`, `/win …`, `/review`. The laptop must be on (or
wake within 3 hours of a brief) for it to be sent.

## Per-org reports (Claude)

Keeps up with your organisations' Messenger group chats for you. It syncs every chat you've
assigned to an org (for example everything named "ECG" or "IMES"), then has Claude read the new
messages and update a set of living documents for that org: a dashboard, a task tracker, a roadmap,
a calendar, a decisions log, open questions and whatever else the chats call for.

```
data/orgs/ecg/
  README.md          dashboard: summary, "For you", next 14 days, what changed, links
  tracker.md         every task: owner, due date, priority, source message
  tasks.json         the same tasks as JSON
  roadmap.md         milestones on a timeline              (created when the chats have them)
  calendar.md        meetings, events, deadlines
  decisions.md       what was agreed, when, by whom
  open-questions.md  unresolved questions and blockers
  people.md, budget.md, events/<event>.md, ...
```

Every item cites the message it came from (`ecg-officers#123` = message #123 in
`data/chats/ecg-officers/transcript.md`), so anything can be checked.

## Setup

Requires Python 3.10, Playwright and the [Claude Code](https://claude.com/claude-code) CLI (`claude`).

```bash
py -3.10 -m pip install -r requirements.txt
py -3.10 -m playwright install chromium
py -3.10 -m unittest discover tests   # rules and bot tests
```

## Choose your orgs and group chats

```bash
py -3.10 hub.py discover     # read your chat list from Messenger (names only, no messages)
py -3.10 hub.py config       # opens the config page at http://127.0.0.1:8765
```

On the config page you:

- set **your name** as it appears in the chats;
- create organisations (ECG, IMES...) and describe each one (what it is, your role), which steers the analysis;
- give an org a **match** pattern (`ECG`, `IMES|IE Talks`) to highlight its likely chats, then **add**
  the ones you want one by one (or all shown at once), or paste a chat link;
- **pause** a chat (skipped by sync and analysis, kept in the org) or **remove** it, and rename it;
- list reports the org should always have (`roadmap, calendar, budget`).

Nothing is added automatically, and nothing is downloaded until you run `sync` or `run`.
Chats inside a Messenger community show as `Community › Chat`.

The same from the command line:

```bash
py -3.10 hub.py org add ecg --name "ECG Organization" --match "ECG" --context "Student org; I'm the VP for internals"
py -3.10 hub.py chat add ecg https://www.facebook.com/messages/t/1234567890 --name "ECG Officers"
py -3.10 hub.py chat disable ecg 1234567890        # or: enable, remove
py -3.10 hub.py discover --add                     # adds every chat matching an org's pattern (bulk)
```

The first browser run opens a Chromium window: log in to Facebook there yourself (and enter your
Messenger PIN if it asks to restore encrypted history). The session is kept in `.profile/`.

## Use it

```bash
py -3.10 hub.py run ecg             # sync ECG's chats, then update data/orgs/ecg/
py -3.10 hub.py run --all          # sync and analyse every org
py -3.10 hub.py status              # what's synced and what hasn't been analysed yet
py -3.10 hub.py vault               # rebuild the Obsidian notes in Hub/ (no tokens, no browser); keep your own notes outside Hub/
```

- **sync** scrolls each enabled chat back only to the last message it already has, appends the new ones
  and downloads their attachments. One browser session handles every chat.
- **analyze** builds `data/orgs/<org>/inbox.md` with only the messages since the last analysis
  (the whole history the first time) and runs `claude -p` with [prompts/analyze.md](prompts/analyze.md).
  Claude may only read, and write inside that org's folder. When it finishes, the org's
  `state.json` records how far it got. If it fails, the inbox stays for the next try.
  `--model`, `--force` (re-run with nothing new) and `--reset` (re-read everything) are available.
- Prefer to watch it work? Open this folder in Claude Code and run `/digest ecg`.

To run it daily, add a Windows Task Scheduler task that runs `run-all.bat` (it needs your
logged-in desktop session, because the browser window is visible).

## Other ways in

- `py -3.10 messenger/scrape.py <thread url> --me <name>` syncs a single chat without an org.
- `py -3.10 messenger/import_export.py <chat folder> --me <name>` converts a chat from Facebook's
  official "Download your information" JSON export (exact timestamps and full-size media, no reply links).
- `scrape.py --debug` keeps raw page rows in `_rows/`; after fixing the extractor,
  `scrape.py --rebuild --out data/chats/<chat>` regenerates the export without the browser.

Caveats: scraped times come from Messenger's labels and are minute-precise at best. Videos in
end-to-end encrypted chats are `blob:` URLs and can't be downloaded. Automated browsing is against
Facebook's terms, so keep runs occasional; the scraper scrolls slowly on purpose.
