# Messenger Hub v2: personal operating system (design proposal)

Status: **approved 2026-10-27. Phase 1 built** (store, rules, Messenger + self-chat capture, app with
Today/Deadlines/Mentions/Review/Wins, Telegram bot with briefs and alerts). Written 2026-10-27 from the interview with Aldrich. Nothing below
is implemented until it's approved; the current code (chat sync + per-org reports) is the starting point.

## 1. The job

Keep Aldrich on track across school, IMES, ECG, Hamster Smash Cup and personal projects, so that
**no deadline, meeting or commitment is missed** and he doesn't have to backread or re-ask.

Failures it must prevent (from the interview, about 10 a month today):

| Failure | Example | What stops it |
| --- | --- | --- |
| Schedules said in chat, never calendared | "GA na sa Thursday 7pm" in a busy GC | Date/time detection on every message → proposed or auto-added event |
| Meeting outcomes not written down | Zoom with no MOTM, a view-only Canva deck afterwards | Deck/link capture + "what was decided" extraction from the chat around it; open question flagged if unclear |
| Own intentions not captured | "I'll do that later tonight" | Messaging yourself (Messenger self-chat, or the Telegram bot) = inbox, picked up automatically |
| Details scattered | Messenger, Telegram, DLSU Gmail, Canvas, Drive, IMC Mastersheet | One store, one timeline, one search |
| Re-asking / not knowing if it was said | "was this already mentioned?" | Ask-the-hub search across every source, answers with the source |
| Losing track of own projects | which repo/folder/Drive file is where | Projects view: last change, unfinished work, days to deadline |

## 2. Who Aldrich is in each org (drives relevance)

| Org | Title | Reports to | Leads | Channels |
| --- | --- | --- | --- | --- |
| **ECG** (Engineering College Government, 17th) | Deputy Chief Communications Officer (Executive Board) | Sam (Isabelle) Reyes, CCO; Ystiphen Lei Dela Cruz, President | Creatives, Publicity & Promotions, Media, Linkages (with Sam) | Messenger `[ECG] …` GCs, Telegram, IMC Mastersheet (Google Sheet, his), weekly alignment meetings (Zoom) |
| **IMES** (Industrial Management Engineering Society, 50th) | AVP for Publicity & Creatives | P&C VP, other VPs, EVPs, President Thirdy Aldevera, fellow AVPs | Junior Officers (recruiting) | Messenger `[50th IMES] …` GCs and the P&C HUB community; Telegram (informal/old) |
| **School** (DLSU, IE-IT, planning to shift to CpE) | Student | — | — | Canvas, ArchersHub, DLSU Gmail (school + org mail) |
| **Hamster Smash Cup** | Lead dev | — | Kim, Patrick, Gab | Messenger "passion projects", GitHub, Google Doc roadmap |
| **Future startup** | — | — | — | (placeholder: business ideas captured via the bot) |

Relevance signals (zero tokens): his name and nicknames, `@Aldrich`, his titles ("DCCO", "AVP P&C",
"pubs", "creatives", "P&C"), the committees he leads, his projects, and messages from people in his
reporting line, weighted higher.

## 3. Sources, in build priority

All connectors are **plain code calling official APIs or the existing browser session**, never
Claude, so syncing costs no tokens.

| # | Source | How | Notes / risks |
| --- | --- | --- | --- |
| 1 | Messenger GCs + **self-chat** (capture inbox) | Existing Playwright scraper | Fragile when Facebook changes markup; ToS grey area, keep runs gentle |
| 2 | Canvas | Canvas REST API with a personal access token | Replaces the static ICS import; deduplicates against the live ICS feed in the personal calendar |
| 3 | Google Calendar (personal + school) | Calendar API (OAuth), read + write | Writes follow the rules in §6 |
| 4 | DLSU Gmail | Gmail API (OAuth), read-only | **Risk:** DLSU's Google Workspace may block third-party OAuth apps. Test first; fallback: a Gmail filter forwarding org/school labels to a personal address |
| 5 | Telegram groups | Telethon (official MTProto API, your account), read-only | Approved. API id/hash from my.telegram.org; session file stays local |
| 6 | Telegram bot | Bot API | The phone surface: briefs, pings, capture, `/win` |
| 7 | ArchersHub | Hub browser, you log in yourself | Class schedule (for "before first class") and term calendar |
| 8 | Google Drive + Sheets | Drive API metadata (names, owners, modified), contents only for linked/project files; Sheets API for IMC Mastersheet | Whole-Drive *metadata* scan, not whole-Drive reading |
| 9 | Projects | `git` on chosen D:\ roots (e.g. `D:\Coding`, `D:\Roblox-Coding`); GitHub API for `aldrichlotas` and `aldrichlotas-dlsu` | Last commit, uncommitted changes, open branches/PRs |
| 10 | Canva / Zoom | Canva view links fetched as text when posted; Zoom only via what's shared | No reliable access to meetings you miss; the system flags "decided in a meeting, no MOTM" as an open question to ask about |

## 4. Data model (SQLite, `data/hub.db`)

```
org         key, name, my_title, term_end, priority, color
person      name, nicknames[], org, title, reports_line (above/peer/below), weight
channel     source, external_id, name, org, kind (exec-board/execom/event/project/self/class),
            analysis_allowed (per-chat consent), detail_level, muted_until
item        raw unit: message | email | event | file | commit | canvas_item; channel, author, ts, text, refs
commitment  title, owner, requester, due, status (todo/doing/waiting/done/dropped),
            stage (request/design/review/approved/posted), org, project, sources[], confidence,
            origin (rule/llm/manual), reviewed (y/n)
event       title, start, end, place/link, org, origin, calendar_state (proposed/added/declined)
project     name, paths[], repos[], drive_folders[], deadline, org
achievement text, date, org/project, evidence[] (commit, post, file), star_draft
suggestion  kind (time_block/decline/delegate/calendar_add), payload, status (open/accepted/rejected)
setting     effort mode, quiet hours, brief times, ping cap, Telegram detail level...
```

The current markdown reports become **views** generated from this store (cheap to regenerate,
no tokens), not the source of truth.

## 5. Processing pipeline and the token dial

```
sync (no tokens) -> item store -> rules layer (no tokens) -> candidates -> model layer (per effort) -> review queue -> planner -> app / bot / calendar
```

**Rules layer, zero tokens:** dates and times in English and Filipino ("bukas", "mamaya",
"sa Friday", "next week", "EOD"); mentions and role titles; GC type from the name; request keywords
("pubmat", "caption", "pa-approve", "deadline", "pasa", "submit", "post"); Canvas due dates;
calendar free/busy; git status.

**Effort dial** (in the app, globally and per chat, with a usage meter):

| Mode | What runs | Model | Typical use |
| --- | --- | --- | --- |
| **Off** | Rules only | none | Default for noisy GCs; zero tokens |
| **Lite** | Model reads only rule-flagged message windows (mentions, dates, requests) | Haiku | Daily default |
| **Standard** | Model reads all new messages in allowed chats, updates commitments | Sonnet | Once a day or on demand |
| **Deep** | Full re-read, weekly-review synthesis, LinkedIn drafts | Opus | Rarely, by button |

Runs use your Claude Pro subscription through `claude -p` (no API bill), so the cost is your
Pro usage limits. Optional: a free local model (Ollama) for Lite, which is less accurate.

**Review queue:** everything the model extracts lands as "unreviewed". One click accepts, edits or
rejects it. Rejections are logged and become test cases (§9).

## 6. Rules for alerts, briefs and calendar writes

**Alerts (Telegram ping):** only (a) something due within 24 h that isn't done, (b) a meeting starting
in 30 min, (c) a direct mention or request from someone in your reporting line with a deadline.
Capped (e.g. 5 a day), collapsed into one message, silent during quiet hours.

**Briefs:** morning (30 min before your first class from the ArchersHub schedule, otherwise 7:30),
afternoon (~13:00) and night (~21:00). Short, skippable, no repeats of what you already saw.
*Changed in phase 1:* a bot can't schedule messages on Telegram's servers (only a user account can),
so a brief is sent when its time comes, or within 3 hours if the laptop was asleep. Scheduling on
Telegram's side can come with the Telethon connection in phase 3.

**Telegram detail level:** titles, due dates and org labels only, e.g. "ECG: pubmat for GA, due
Thu 18:00". Message text, names of others and file links stay on the laptop. Configurable per channel.

**Calendar:** added automatically: class schedule, Canvas due dates, and *confirmed meetings*
(date + time + place or link, posted by someone in your reporting line or acknowledged by you).
Everything else is a **proposal** you accept in the app or with one tap in Telegram.
Time blocks are suggested next to deadlines, never written without a tap.

**Decline / delegate:** flagged when the week's committed hours exceed your available hours, when
a request is low-priority for you, or when it's JO-level work (IMES) or committee-level work (ECG)
someone below you could take.

## 7. Weekly review, at your own pace

There's no fixed time. A **Review** button runs a prepared 5-minute flow: close what's done, accept or
reject pending proposals, look at the next 14 days, log wins. If you haven't reviewed in 10 days, the
next brief adds one gentle line. Nothing more.

## 8. The laptop app

- A **desktop window that starts with Windows** (pywebview: a native window around the local web UI,
  using Windows' built-in Edge WebView2), plus a tray icon. A **floating bar** (a frameless strip, always on top, with sync status and the next
  thing) replaces the sidebar and widget modes, which were dropped on 2026-10-31.
- Layout:
  - top: **Today** (timeline: classes, meetings, due) and **Projects**, side by side
  - then **Week** (calendar shaded by load)
  - side by side: **Deadline countdown**, **Load** (committed vs available hours per org), **Per-org boards** (request → design → review → approved → posted)
  - **Mentions** feed, prominent, with "mark read" so backreading stops
  - **Ask** box: "was the GA venue already announced?" answers from local search first (no tokens), model only if you ask it to
- **All configuration by clicking:** orgs, people and titles, chats and per-chat consent/effort/detail,
  brief times, quiet hours, ping cap, calendar rules, project folders.

## 9. How we'll know it's right

| Check | Method | Target |
| --- | --- | --- |
| Extraction accuracy | You label ~150 messages (task / deadline / meeting / none) on a click-through page, sampled across orgs and GC types; score rules-only and each effort mode | Catch ≥ 90% of real deadlines; ≥ 70% of what it flags is real |
| Real outcome | Missed or nearly missed commitments: baseline about 10 in the last month; count for 4 weeks after go-live (`/missed` in the bot logs one) | ≤ 3 a month |
| Org reality | Sam (or another officer) checks the ECG board once | "Matches reality" with ≤ 2 corrections |
| Calendar truth | Auto-added and proposed events compared with what actually happened | No wrong auto-added events |
| Token use | Usage meter per mode | Lite fits comfortably inside Pro limits daily |

## 10. LinkedIn

Goal: credibility for internships and long-term entrepreneurship (IE-IT now, planning CpE).
LinkedIn has **no API for editing your own profile**, so the hub keeps an **achievements ledger**
(`/win` notes + commits + event outcomes, each with evidence) and drafts STAR-format bullets and
section updates. You approve them and paste them in, or the hub browser fills them in one at a
time with your approval. A one-time review of the current profile comes first.

## 11. Build phases (each ends with something you use)

1. **Core:** SQLite store, Messenger + self-chat capture, rules layer, app with Today + Deadlines + Mentions, Telegram bot with briefs. *Measured against the labelled set.*
2. **School:** Canvas API, Google Calendar (dedupe, auto-add rules), ArchersHub schedule.
3. **More channels:** Telegram groups (Telethon), DLSU Gmail (OAuth test first), IMC Mastersheet.
4. **Projects:** git across chosen D:\ roots, both GitHub accounts, Drive metadata.
5. **Planning:** Week + Load views, time-block, decline and delegate suggestions, per-org boards.
6. **Growth:** achievements ledger, `/win`, LinkedIn drafts, Ask box.

## 12. Open decisions

See the summary at the end of the interview; recorded here once decided.
