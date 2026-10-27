# CLAUDE.md

**Messenger Hub** syncs Messenger group chats, grouped into organisations (ECG, IMES...), and keeps
per-org living documents (dashboard, tracker, roadmap...) up to date with Claude. User guide: [README.md](README.md).

## Layout

```
hub.py                 CLI: app / process / config / org / chat / discover / sync / analyze / run / status
hubos/store.py         SQLite store data/hub.db: item (messages), entry (deadline/meeting/task/capture/win), mention, sent
hubos/rules.py         zero-token rules: find_date (English + Filipino), mention, analyse, capture; tests in tests/
hubos/process.py       ingest data/chats/* into the store, derive entries/mentions; dashboard() feeds app and bot
hubos/bot.py           Telegram bot (pairing, capture, commands) + scheduler (briefs, alerts, quiet hours, cap)
hubos/app.py + ui/app.html   `hub.py app`: local server, background processor, pywebview window, startup toggle
data/secrets.json      bot token, paired chat id, pair code (git-ignored with the rest of data/)
.env                   optional, git-ignored: TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID override secrets.json (no pairing); TELEGRAM_API_ID/HASH for phase 3
config_ui.py + ui/index.html   `hub.py config`: local (127.0.0.1) page for picking orgs' chats
orgs.json              config (git-ignored): {"me", "profile": {names, self_chat, self_slug}, "settings": {briefs, quiet, alerts, ping_cap, detail},
                       "orgs": {key: {name, match, context, outputs, my_title, aliases, committees, above, chats: [{thread, name, slug, enabled}]}}}
messenger/scrape.py    Playwright scraper: launch() + scrape_thread(); incremental via meta.json "last_exported"
messenger/extract.js   in-page message extractor (ARIA/data-scope selectors only; Facebook obfuscates classes)
messenger/discover.js  in-page chat-list collector for `hub.py discover`
messenger/common.py    export format: messages.jsonl, transcript.md, meta.json; reply linking; format_message()
messenger/import_export.py   Facebook "Download your information" JSON -> same export format
prompts/analyze.md     the analysis brief; {{PLACEHOLDERS}} are filled by hub.fill_prompt()
data/chats/<slug>/     one export per chat (shared between orgs)
data/orgs/<org>/       reports + state.json (last analysed message id per chat) + inbox.md while pending
.profile/              the logged-in Chromium profile
```

`data/`, `orgs.json`, `.profile/` and `STOP` are git-ignored: chat content never goes into git.

## Working here

- **Never commit or push chat content, `orgs.json` or `.profile/`**, and don't paste chat contents
  into anything that leaves this machine unless the user asks.
- **The user chooses which chats belong to an org** (config page or `chat add`). Never add chats
  automatically, and don't sync until asked.
- The browser commands (`discover`, `sync`, `run`) open a visible Chromium window and need the user
  at the desk if Facebook asks for a login or PIN. Don't start them unasked.
- `/digest <org>` runs the analysis interactively in this session; `hub.py analyze` runs the same
  brief headless via `claude -p --permission-mode acceptEdits --allowedTools Read,Write,Edit,Glob,Grep`.
- When analysing, follow `prompts/analyze.md`: update existing documents rather than rewriting them,
  keep task IDs stable, and cite `<chat>#<id>` for everything.
- Facebook changes its markup now and then. If extraction breaks, sync with `--debug`, inspect
  `data/chats/<chat>/_rows/`, fix `extract.js` / `scrape.to_messages`, then `scrape.py --rebuild`.
- Python is `py -3.10` (Playwright and pywebview are installed there). Run `py -3.10 -m unittest discover tests`
  after touching `hubos/`.
- The rules layer must stay token-free. New date words, cue words or false positives go in `rules.py`
  with a test case in `tests/test_rules.py`.
- Telegram text must never carry chat message bodies, other people's names, links or contact details:
  go through `bot.line()` / `bot.safe_title()`.
- `docs/DESIGN.md` is the approved plan (phases 1–6). Phase 1 (store, rules, app, bot) is built.

## Origin

Copied on 2026-10-15 from the Roblox repo's local-only `tools/messenger/` (which still exists there,
syncing the "passion projects" team chat for that game). Changes made there aren't reflected here
automatically.
