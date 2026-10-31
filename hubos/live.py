"""Live sync: while the hub runs, watch the Messenger chat list and fetch only the chats that changed.

One worker owns the browser profile, and the Sync button goes through its queue. It reads the chat list
every few seconds without opening a chat (messenger/watch.js); a chat opens in a tab of its own when its row
changes. A slow catch-up pass walks every chat for what the list can't show: the list holds about 17 rows, a
new message with the same text as the last one looks unchanged, and community chats may not appear at all.

A login, PIN or checkpoint page pauses the worker. Only the Resume button clears that, across restarts too:
it never retries through one. Nothing is opened past the daily limit. Quiet hours are for Telegram: chats still open.

Off by default (settings "live"). Everything it logs goes to data/live/ as times, thread ids and counts: no
message text. `hub.py live` reports the delay, chats opened per day and checkpoints from those logs.
"""

import collections
import datetime as dt
import json
import os
import random
import shutil
import threading
import time
import urllib.request

import common
import hub
import scrape

from . import process, settings
from .store import ROOT

DIR = os.path.join(ROOT, "data", "live")
START_URL = "https://www.facebook.com/messages/new/"  # lists chats with none open
TICK = 5  # seconds between reads of the chat list
OPEN_GAP = (8, 20)  # seconds between opening any two chats
COOLDOWN = (45, 90)  # seconds before the same chat opens again: a burst of messages costs one or two opens
CATCHUP_HOURS = 3  # every chat gets looked at this often, spread out evenly, even if its row never moved
BACKOFF = (30, 1800)  # seconds after a failure, doubling up to the second number
FAILS_TO_PAUSE = 3  # chats failing to load in a row: something on the page probably wants a human
EMPTY_TO_PAUSE = 24  # ticks with an empty chat list (2 minutes)
PATIENCE = 45  # seconds a chat gets to load
FETCH_MINUTES = 3  # a chat gets this long to read back to its last saved message
BACKLOG_MINUTES = 30  # one longer go for a chat that ran out of time, e.g. after days away
REFRESH = (45, 75)  # minutes between reloads of the chat list, in case the page has quietly gone stale
ASLEEP = 60  # seconds between ticks that mean the laptop slept: reload rather than trust the page
CLUMP = 4  # this many rows changing at once isn't messages: time labels the signature failed to ignore
TOP = 3  # rows counted as the top of the list, where a new message lands
BOLD = 600  # font weight that means unread
AUDIT_HOURS = 24
AUDIT_SETTLE = 10  # minutes: leave out messages too new for the worker to have fetched yet


def backoff(fails: int, rng=random) -> float:
    """Seconds to wait after this many failures in a row: doubling, capped, with jitter."""
    return min(BACKOFF[1], BACKOFF[0] * 2 ** max(0, fails - 1)) * rng.uniform(0.5, 1)


def percentile(values: list, p: float):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(len(v) * p / 100 + 0.999999) - 1))]


class Detector:
    """Which chats changed, from successive reads of the list. Rows hold only ids, hashes and weights."""

    def __init__(self):
        self.known = {}  # id -> (sig, weight)
        self.noise = 0  # clumped rows left for the catch-up pass

    def read(self, rows: list) -> list:
        first, changed, rising = not self.known, [], set()
        ranks = {r["id"]: r.get("rank", 99) for r in rows}
        for r in rows:
            before = self.known.get(r["id"])
            if first:  # what arrived while the hub was closed
                if r["weight"] >= BOLD:
                    changed.append(r["id"])
            elif before is None:  # never seen: scrolled into view, unless it landed on top
                if ranks[r["id"]] < TOP:
                    changed.append(r["id"])
            elif before[0] != r["sig"]:
                changed.append(r["id"])
            elif r["weight"] >= BOLD > before[1]:
                changed.append(r["id"])
                rising.add(r["id"])
            self.known[r["id"]] = (r["sig"], r["weight"])
        if not first and len(changed) >= CLUMP:
            kept = [i for i in changed if ranks[i] < TOP or i in rising]
            self.noise += len(changed) - len(kept)
            return kept
        return changed


class Pacer:
    """What to open next: changed chats first, then the catch-up pass, never too fast."""

    def __init__(self, rng=random):
        self.rng = rng
        self.pending = {}  # id -> why, oldest first
        self.cool = {}  # id -> time it may open again
        self.next_open = 0.0
        self.next_catchup = 0.0

    def add(self, chat: str, why: str) -> None:
        self.pending.setdefault(chat, why)

    def next(self, now: float):
        """The chat to open now and why, or None. The caller says opened() when it has."""
        if now < self.next_open:
            return None
        for chat, why in self.pending.items():
            if now >= self.cool.get(chat, 0):
                del self.pending[chat]
                return chat, why
        return None

    def catchup(self, now: float, chats: list, checked: dict):
        """The chat looked at longest ago, once its turn in the slow pass comes round."""
        if not chats or now < self.next_open or now < self.next_catchup:
            return None
        free = [c for c in chats if c not in self.pending and now >= self.cool.get(c, 0)]
        if not free:
            return None
        self.next_catchup = now + CATCHUP_HOURS * 3600 / len(chats) * self.rng.uniform(0.8, 1.2)
        return min(free, key=lambda c: checked.get(c, ""))

    def opened(self, chat: str, now: float) -> None:
        self.next_open = now + self.rng.uniform(*OPEN_GAP)
        self.cool[chat] = now + self.rng.uniform(*COOLDOWN)


class Browser:
    """The real thing: the chat list in one tab, each chat in a short-lived tab of its own."""

    def __init__(self):
        from playwright.sync_api import sync_playwright

        with open(os.path.join(scrape.HERE, "watch.js"), encoding="utf-8") as f:
            self.script = f.read()
        self.pw = sync_playwright().start()
        try:
            self.context = scrape.launch(self.pw, minimized=True)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(START_URL, wait_until="domcontentloaded")
        except Exception:
            self.pw.stop()
            raise

    def look(self) -> dict:
        why = scrape.blocked(self.page, self.context)
        if why:
            return {"blocked": why, "rows": []}
        self.page.evaluate(self.script)  # does nothing if it's already there
        return {"blocked": "", "rows": self.page.evaluate("window.__hsw.drain().rows")}

    def reload(self) -> None:
        self.page.goto(START_URL, wait_until="domcontentloaded")

    def fetch(self, chat: dict, me: str, out_dir: str, since: str = None, no_download: bool = False, minutes: float = FETCH_MINUTES) -> dict:
        tab = self.context.new_page()
        try:
            return scrape.scrape_thread(self.context, chat["thread"], out_dir=out_dir, me=me, since=since, no_download=no_download,
                                        max_minutes=minutes, page=tab, patience=PATIENCE, strict=True)
        finally:
            tab.close()

    def show(self) -> None:
        self.page.bring_to_front()

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.pw.stop()


class Worker:
    def __init__(self, store, load_config=hub.load_config, notice=None, plain_sync=None, browser=Browser, folder=DIR,
                 now=dt.datetime.now, rng=random):
        self.store, self.load_config, self.notice, self.plain_sync = store, load_config, notice, plain_sync
        self.make_browser, self.folder, self.now, self.rng = browser, folder, now, rng
        self.browser = None
        self.detector = Detector()
        self.pacer = Pacer(rng)
        self.jobs = collections.deque()
        self.syncing = self.every_chat = False
        self.started = now()
        self.last_check = None
        self.last_tick = None
        self.next_refresh = None
        self.retry_at = None
        self.fails = self.empty = 0
        self.unsent = None  # a notice still waiting for quiet hours to end
        self.in_list = set()
        self.backlog = set()  # chats that ran out of time: their next go gets BACKLOG_MINUTES
        self.held = None  # why nothing opens right now, when it clears by itself
        self.running_state = "stopped"
        self.rest = time.sleep
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread = None
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "state.json")
        self.saved = {"day": None, "opens": 0, "manual": 0, "paused": None, "checked": {}}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.saved.update(json.load(f))

    # --- what the app calls ------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        self._wake.set()
        if self._thread:
            self._thread.join(8)

    def wake(self) -> None:
        self._wake.set()

    def request_sync(self, every_chat: bool = False) -> None:
        """The Sync button. Refused while paused: that would walk straight back into the page that stopped us."""
        if self.saved["paused"] and settings.get(self.load_config())["live"]:
            raise ValueError(f"Live sync is paused: {self.saved['paused']}. Sort that out, then press Resume.")
        if not self.syncing:
            hub.clear_stop()
            self.syncing, self.every_chat = True, every_chat
            self.jobs.append("sync")
            self.wake()

    def stop_sync(self) -> None:
        """The Stop button: a new chat keeps what's been read, a chat already synced reads again next time, no more open."""
        if "sync" in self.jobs:
            self.jobs.remove("sync")
            self.syncing = False
        elif self.syncing:
            with open(hub.STOP_FILE, "w", encoding="utf-8"):
                pass

    def request_audit(self) -> None:
        if not settings.get(self.load_config())["live"]:
            raise ValueError("Turn live sync on first: the comparison runs in its browser.")
        if self.saved["paused"]:
            raise ValueError(f"Live sync is paused: {self.saved['paused']}.")
        if "audit" not in self.jobs:
            self.jobs.append("audit")
            self.wake()

    def resume(self) -> None:
        self.saved["paused"] = None
        self.fails = self.empty = 0
        self.retry_at = self.unsent = self.held = None
        self.running_state = "starting"
        self._save()
        self._log("resume")
        self.wake()

    def status(self) -> dict:
        self._new_day()
        cfg = self.load_config()
        sset = settings.get(cfg)
        paused = self.saved["paused"] if sset["live"] else None
        state = "stopped" if not sset["live"] else "paused" if paused else self.running_state
        return {
            "state": state,
            "why": paused or self.held,
            "needs_you": bool(paused),
            "last_check": self.last_check.isoformat(timespec="seconds") if self.last_check else None,
            "opens_today": self.saved["opens"],
            "limit": int(sset["live_limit"]),
            "waiting": len(self.pacer.pending),
        }

    # --- the loop ----------------------------------------------------------------------------------

    def run(self) -> None:
        while not self._quit.is_set():
            try:
                self.step()
            except Exception as e:  # never take the app down with it
                print(f"[live] {e}")
                self._fail(str(e))
            self._wake.wait(TICK)
            self._wake.clear()
        self._close()

    def step(self) -> None:
        cfg = self.load_config()
        sset = settings.get(cfg)
        now = self.now()
        self._new_day()
        self._send_notice()
        if not sset["live"]:
            self._close()
            self.running_state, self.held = "stopped", None
            if self.jobs:
                self._job_without_browser(self.jobs.popleft())
            return
        if self.saved["paused"]:
            self.jobs.clear()
            self.syncing = False
            return
        if self.retry_at and now < self.retry_at:
            return
        if not self.browser:
            self.running_state = "starting"
            self.browser = self.make_browser()
            self.next_refresh = None
            self._log("start")
        look = self.browser.look()
        if look["blocked"]:
            return self._pause(f"Facebook wants you to sign in or confirm it's you ({look['blocked']})", checkpoint=True)
        if not look["rows"]:
            self.empty += 1
            if self.empty >= EMPTY_TO_PAUSE:
                self._pause("The chat list isn't showing. Messenger may want your PIN")
            return
        self.empty = 0
        self.running_state = "live"
        self.last_check = now
        slept = self.last_tick and (now - self.last_tick).total_seconds() > ASLEEP
        self.last_tick = now
        if self.next_refresh is None:
            self.next_refresh = now + dt.timedelta(minutes=self.rng.uniform(*REFRESH))
        if slept or now >= self.next_refresh:
            self.next_refresh = None
            self.browser.reload()
            return
        targets = {hub.thread_id(c["thread"]): c for c in hub.sync_targets(cfg, list(cfg["orgs"]))}
        self.in_list |= {r["id"] for r in look["rows"]} & set(targets)
        for chat in self.detector.read(look["rows"]):
            if chat in targets:
                self.pacer.add(chat, "list")
        if self.jobs:
            self._job_in_browser(self.jobs.popleft(), cfg, list(targets.values()))
            return
        self.held = self._why_held(sset)
        if self.held:
            return
        clock = now.timestamp()
        pick = self.pacer.next(clock)
        if not pick:
            chat = self.pacer.catchup(clock, list(targets), self.saved["checked"])
            pick = (chat, "catchup") if chat else None
        if pick:
            chat, why = pick
            if chat in targets:
                self._open(targets[chat], chat, why, cfg)

    def _why_held(self, sset: dict):
        if self.saved["opens"] >= int(sset["live_limit"]):
            return "Daily limit reached: chats that change wait until tomorrow"
        return None

    def _open(self, c: dict, chat: str, why: str, cfg: dict) -> bool:
        """Fetch one chat into the store. False if it didn't work; chats from the list go back in the queue."""
        started = self.now()
        try:
            res = self.browser.fetch(c, cfg["me"], hub.chat_dir(c), c.get("since"),
                                     minutes=BACKLOG_MINUTES if chat in self.backlog else FETCH_MINUTES)
        except scrape.Blocked as e:
            self._pause(f"Facebook wants you to sign in or confirm it's you ({e})", checkpoint=True)
            return False
        except Exception as e:
            if why == "list":
                self.pacer.add(chat, why)
            self._fail(str(e), loading=True)
            return False
        self.fails, self.retry_at = 0, None
        self._count(why)
        if res.get("dropped"):
            return self._dropped(chat, why, res["dropped"])
        self.backlog.discard(chat)
        self.pacer.opened(chat, self.now().timestamp())
        hub.record_chat(self.load_config(), c, res)
        process.ingest(self.store, self.load_config())
        stored = self.now()
        self.saved["checked"][chat] = stored.isoformat(timespec="seconds")
        self._save()
        fresh = common.load_messages(res["out_dir"])[-res["added"]:] if res["added"] else []
        lags = [(stored - t).total_seconds() for t in (dt.datetime.fromisoformat(m["ts"]) for m in fresh if m.get("ts")) if t >= self.started]
        self._log("open", chat=chat, why=why, added=res["added"], lags=lags, took=round((stored - started).total_seconds(), 1), in_list=chat in self.in_list)
        return True

    def _dropped(self, chat: str, why: str, reason: str) -> bool:
        """The read ended before the last saved message, so nothing was saved: the chat needs another go."""
        self._log("dropped", chat=chat, why=why, reason=reason)
        if reason == "limit":
            if chat in self.backlog:
                self.backlog.discard(chat)
                self._pause("A chat has too much to catch up on here. Turn live sync off and press Sync")
                return False
            self.backlog.add(chat)
        if why == "list":
            self.pacer.add(chat, why)
        return False

    def _job_without_browser(self, job: str) -> None:
        try:
            if job == "sync" and self.plain_sync:
                self.plain_sync(self.every_chat)
        finally:
            hub.clear_stop()
            self.syncing = False

    def _job_in_browser(self, job: str, cfg: dict, chats: list) -> None:
        try:
            if job == "audit":
                self._audit(cfg, chats)
                return
            for c in chats:
                self._open(c, hub.thread_id(c["thread"]), "manual", cfg)
                if self.saved["paused"] or os.path.exists(hub.STOP_FILE):
                    break
        finally:
            hub.clear_stop()
            self.syncing = False

    def _audit(self, cfg: dict, chats: list) -> dict:
        """Read the last 24 hours of every chat a second time, apart from the export, and compare the two."""
        now = self.now()
        since = (now - dt.timedelta(hours=AUDIT_HOURS)).isoformat(timespec="minutes")
        before = (now - dt.timedelta(minutes=AUDIT_SETTLE)).isoformat(timespec="minutes")
        base = os.path.join(self.folder, "audit")
        missed, read = {}, 0
        for c in chats:
            chat = hub.thread_id(c["thread"])
            out = os.path.join(base, chat)
            shutil.rmtree(out, ignore_errors=True)
            try:
                res = self.browser.fetch(c, cfg["me"], out, since=since, no_download=True)
            except scrape.Blocked as e:
                self._pause(f"Facebook wants you to sign in or confirm it's you ({e})", checkpoint=True)
                return {}
            except Exception as e:
                missed[chat] = f"not read: {e}"[:120]
                continue
            self._count("manual")
            window = lambda ms: [common.message_key(m) for m in ms if m.get("ts") and since <= m["ts"] < before]
            second = collections.Counter(window(common.load_messages(res["out_dir"])))
            kept = collections.Counter(window(common.load_messages(hub.chat_dir(c) or "")))
            read += sum(second.values())
            gap = sum((second - kept).values())
            if gap:
                missed[chat] = gap
            shutil.rmtree(out, ignore_errors=True)
            self.rest(self.rng.uniform(*OPEN_GAP))
        out = {"at": now.isoformat(timespec="seconds"), "since": since, "before": before, "chats": len(chats), "read": read, "missed": missed}
        with open(os.path.join(self.folder, "audit.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        self._log("audit", read=read, missed=missed)
        return out

    # --- trouble -----------------------------------------------------------------------------------

    def _pause(self, why: str, checkpoint: bool = False) -> None:
        self.saved["paused"] = why
        self.running_state = "paused"
        self._save()
        self._log("pause", why=why, checkpoint=checkpoint)
        self.unsent = "Messenger Hub: live sync paused. Open the app to see why, then press Resume."
        self._send_notice()
        if self.browser:
            try:
                self.browser.show()
            except Exception:
                pass

    def _fail(self, error: str, loading: bool = False) -> None:
        offline = "net::" in error
        self.fails += 1
        self._log("fail", loading=loading, offline=offline)
        if loading and not offline and self.fails >= FAILS_TO_PAUSE:
            return self._pause("Chats have stopped loading. Messenger may want your PIN")
        self.retry_at = self.now() + dt.timedelta(seconds=backoff(self.fails, self.rng))
        self.held = "Couldn't reach Messenger: trying again soon"
        if not loading:  # the list itself broke: start again with a new browser
            self._close()

    def _close(self) -> None:
        if self.browser:
            try:
                self.browser.close()
            except Exception as e:
                print(f"[live] closing: {e}")
            self.browser = None
            self._log("stop")

    def _send_notice(self) -> None:
        if self.unsent and self.notice and self.notice(self.unsent):
            self.unsent = None

    # --- records -----------------------------------------------------------------------------------

    def _new_day(self) -> None:
        today = self.now().date().isoformat()
        if self.saved["day"] != today:
            self.saved.update(day=today, opens=0, manual=0)
            self._save()

    def _count(self, why: str) -> None:
        self.saved["manual" if why == "manual" else "opens"] += 1

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.saved, f, indent=1)
        os.replace(tmp, self.path)

    def _log(self, kind: str, **data) -> None:
        now = self.now()
        with open(os.path.join(self.folder, now.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now.isoformat(timespec="seconds"), "kind": kind, **data}) + "\n")


def report(days: int = 7, folder: str = DIR, today: dt.date = None) -> dict:
    """Delay from Messenger's own timestamps to the store, chats opened per day, and every pause.
    Messenger shows whole minutes, so a delay reads up to 60 seconds too long."""
    today = today or dt.date.today()
    lags, opens, pauses, checkpoints, fails = collections.defaultdict(list), collections.defaultdict(collections.Counter), [], 0, 0
    seen, chats_open = set(), set()
    for n in range(days):
        day = (today - dt.timedelta(days=n)).isoformat()
        path = os.path.join(folder, day + ".jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r["kind"] == "open":
                    opens[day][r["why"]] += 1
                    lags[r["why"]] += r.get("lags") or []
                    chats_open.add(r["chat"])
                    if r.get("in_list"):
                        seen.add(r["chat"])
                elif r["kind"] == "pause":
                    pauses.append({"at": r["at"], "why": r["why"]})
                    checkpoints += bool(r.get("checkpoint"))
                elif r["kind"] == "fail":
                    fails += 1
    every = [x for v in lags.values() for x in v]
    delay = {why: {"messages": len(v), "p50_s": percentile(v, 50), "p95_s": percentile(v, 95)} for why, v in sorted(lags.items())}
    audit = os.path.join(folder, "audit.json")
    return {
        "delay": delay,
        "all_messages": {"messages": len(every), "p50_s": percentile(every, 50), "p95_s": percentile(every, 95),
                         "within_2_min": round(sum(x <= 120 for x in every) / len(every), 3) if every else None},
        "opens_per_day": {d: dict(c) for d, c in sorted(opens.items())},
        "checkpoints": checkpoints,
        "pauses": pauses,
        "failures": fails,
        "chats_seen_in_list": len(seen),
        "chats_opened": len(chats_open),
        "audit": json.load(open(audit, encoding="utf-8")) if os.path.exists(audit) else None,
    }


def ask_audit(port: int = 8765) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/app/live-audit", data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        return "Comparison queued in the running hub. See it in `hub.py live` when it's done."
