"""Zero-token rules: find dates, mentions of you, and things to act on in a message.

Pure functions (no I/O), unit-tested in tests/test_rules.py. Tuned for Philippine org chats:
English and Filipino date words ("bukas", "mamaya", "sa Biyernes"), Taglish request words
("pa-approve", "pasa"), and org roles ("DCCO", "AVP P&C").
"""

import datetime as dt
import re
from dataclasses import dataclass, field

# --- dates --------------------------------------------------------------------------------------

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {
    "monday": 0, "lunes": 0, "tuesday": 1, "martes": 1, "wednesday": 2, "miyerkules": 2, "miyerkoles": 2,
    "thursday": 3, "huwebes": 3, "friday": 4, "biyernes": 4, "saturday": 5, "sabado": 5, "sunday": 6,
}
# Abbreviations only count when capitalised ("Fri", not "sat" as in a word).
WEEKDAY_ABBR = {"Mon": 0, "Tue": 1, "Tues": 1, "Wed": 2, "Thu": 3, "Thur": 3, "Thurs": 3, "Fri": 4, "Sat": 5, "Sun": 6}

_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|May|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
MONTH_DAY = re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(20\d\d))?", re.I)
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\b\.?(?:,?\s*(20\d\d))?", re.I)
NUMERIC = re.compile(r"(?<![\d/])(1[0-2]|0?[1-9])/(3[01]|[12]\d|0?[1-9])(?:/(\d{2}|\d{4}))?(?![\d/])")
TIME_AMPM = re.compile(r"\b(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*([ap])\.?\s?m\b\.?", re.I)
TIME_24 = re.compile(r"(?<![\d/:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
TIME_WORD = re.compile(r"\b(noon|tanghali|midnight|hatinggabi|eod|end of (?:the )?day)\b", re.I)
REL_DAY = [
    (re.compile(r"\b(day after tomorrow|sa makalawa|makalawa)\b", re.I), 2),
    (re.compile(r"\b(tomorrow|tmrw|tmr|bukas)\b", re.I), 1),
    (re.compile(r"\b(today|tonight|later today|ngayong araw|ngayon|mamayang gabi|mamaya)\b", re.I), 0),
]
NEXT_WEEK = re.compile(r"\b(next week|next wk|susunod na linggo|sa susunod na linggo)\b", re.I)
_WD_PREFIX = r"\b((?i:next|this coming|this|sa|on|ngayong)\s+)?"
WEEKDAY_RES = (
    re.compile(
        _WD_PREFIX + r"((?i:monday|lunes|tuesday|martes|wednesday|miyerkules|miyerkoles|thursday|huwebes|friday|biyernes"
        r"|saturday|sabado|sunday))\b"
    ),
    re.compile(_WD_PREFIX + r"(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b\.?"),
)
# A day of the month without a month: "26th", "the 28th", "28 wednesday", "Wed the 28th".
_WDN = r"(?i:monday|lunes|tuesday|martes|wednesday|miyerkules|miyerkoles|thursday|huwebes|friday|biyernes|saturday|sabado|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)"
DAY_ONLY = (
    re.compile(r"\b(3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?\s+" + _WDN + r"\b"),
    re.compile(_WDN + r",?\s+(?:the\s+)?(3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?\b"),
    re.compile(
        r"\b(?:on|by|until|til|till|before|sa|due|deadline|hanggang)\s+(?:the\s+)?(3[01]|[12]\d|0?[1-9])(st|nd|rd|th)\b"
        r"(?!\s+(?:place|draft|time|try|round|week|year|batch|pubmats?|posts?|floor|version|gen|ecg|imes|term|sem))",
        re.I,
    ),
)
PAST_WORDS = re.compile(r"\b(last|kahapon|yesterday|kanina|earlier|nung|noong)\b", re.I)


@dataclass
class DateHit:
    when: dt.datetime
    has_time: bool
    text: str


def _find_time(text: str):
    """(hour, minute, matched text) for the first time of day in text, or None."""
    m = TIME_AMPM.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3).lower() == "p" and h < 12:
            h += 12
        if m.group(3).lower() == "a" and h == 12:
            h = 0
        return h, mi, m.group(0)
    m = TIME_24.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 1 <= h <= 6:  # "5:30" in a chat is almost always 5:30 PM
            h += 12
        return h, mi, m.group(0)
    m = TIME_WORD.search(text)
    if m:
        w = m.group(1).lower()
        return (12, 0, m.group(0)) if w in ("noon", "tanghali") else (23, 59, m.group(0))
    return None


def _year_for(month: int, day: int, base: dt.date, year=None):
    if year:
        y = int(year)
        return y + 2000 if y < 100 else y
    try:
        d = dt.date(base.year, month, day)
    except ValueError:
        return None
    # "Jan 5" written in December means next January.
    return base.year + 1 if d < base - dt.timedelta(days=60) else base.year


def _weekday_date(m, base: dt.date):
    word = m.group(2)
    wd = WEEKDAYS.get(word.lower()) if word.lower() in WEEKDAYS else WEEKDAY_ABBR.get(word)
    if wd is None:
        return None
    ahead = (wd - base.weekday()) % 7
    d = base + dt.timedelta(days=ahead)
    if (m.group(1) or "").strip().lower() == "next":
        # "next Friday": Friday of next week when this week's Friday is still to come.
        if ahead > 0 and d.isocalendar()[1] == base.isocalendar()[1]:
            d += dt.timedelta(days=7)
    return d


def find_date(text: str, base: dt.datetime):
    """The first future-looking date/time a message refers to, resolved against when it was sent."""
    if not text:
        return None
    day, matched = None, ""
    bd = base.date()
    for rx in (MONTH_DAY, DAY_MONTH):
        m = rx.search(text)
        if m:
            g = m.groups()
            mon, d, y = (g[0], g[1], g[2]) if rx is MONTH_DAY else (g[1], g[0], g[2])
            if mon.lower() == "may" and not mon[0].isupper():
                continue  # Filipino "may" = "there is"
            month = MONTHS.get(mon.lower().rstrip("."))
            year = _year_for(month, int(d), bd, y) if month else None
            if year:
                try:
                    day, matched = dt.date(year, month, int(d)), m.group(0)
                    break
                except ValueError:
                    pass
    if day is None:
        m = NUMERIC.search(text)
        if m:
            month, d = int(m.group(1)), int(m.group(2))
            year = _year_for(month, d, bd, m.group(3))
            if year:
                try:
                    day, matched = dt.date(year, month, d), m.group(0)
                except ValueError:
                    pass
    if day is None:
        for rx in DAY_ONLY:
            m = rx.search(text)
            if m:
                d = int(m.group(1))
                # The next date with that day of the month.
                y, mo = bd.year, bd.month
                if d < bd.day:
                    y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
                try:
                    day, matched = dt.date(y, mo, d), m.group(0)
                    break
                except ValueError:
                    pass
    if day is None:
        found = sorted((m for rx in WEEKDAY_RES for m in rx.finditer(text)), key=lambda m: m.start())
        for m in found:
            before = text[max(0, m.start() - 12) : m.start()]
            if PAST_WORDS.search(before):
                continue
            d = _weekday_date(m, bd)
            if d:
                day, matched = d, m.group(0).strip()
                break
    if day is None:
        for rx, offset in REL_DAY:
            m = rx.search(text)
            if m:
                # After midnight, "tomorrow" still means the coming day you haven't slept yet.
                if offset >= 1 and base.hour < 4:
                    offset -= 1
                day, matched = bd + dt.timedelta(days=offset), m.group(0)
                break
    if day is None:
        m = NEXT_WEEK.search(text)
        if m:
            day, matched = bd + dt.timedelta(days=7 - bd.weekday()), m.group(0)
    t = _find_time(text)
    if day is None and t is None:
        return None
    if day is None:
        # A bare time ("7pm") means today, or tomorrow if that time has already passed.
        day = bd if (t[0], t[1]) >= (base.hour, base.minute) else bd + dt.timedelta(days=1)
    if t:
        return DateHit(dt.datetime(day.year, day.month, day.day, t[0], t[1]), True, (matched + " " + t[2]).strip())
    return DateHit(dt.datetime(day.year, day.month, day.day, 23, 59), False, matched)


# --- cues ---------------------------------------------------------------------------------------

DEADLINE = re.compile(
    r"\b(deadline|dl|due|until|til|till|before|by|pasa|ipasa|ipapasa|submit|submission|hanggang|cut-?off|eod|latest"
    r"|launch|release|goes public|go live|go public|publish|deploy|deliver|delivery)\b",
    re.I,
)
MEETING = re.compile(
    r"\b(meeting|meet|mtg|ga|general assembly|call|zoom|gmeet|google meet|huddle|alignment|assembly|orientation"
    r"|briefing|convocation|kita tayo|pulong|onsite|on-site|f2f)\b",
    re.I,
)
REQUEST = re.compile(
    r"\b(pubmats?|captions?|posters?|layouts?|graphics?|designs?|edit|pa-?approve|approve|approval|review|pa-?check"
    r"|post|posting|upload|pa-?post|teasers?|reels?|banners?|cover photo|deck|ppt|slides|forms?|pa-?send|send|pa-?gawa|gawa)\b",
    re.I,
)
PLACE = re.compile(r"(zoom\.us|meet\.google|teams\.microsoft|\b(room|venue|hall|gk|velasco|henry sy|andrew|lobby|via zoom|online)\b)", re.I)
MY_COMMIT = re.compile(
    r"\b(i'?ll|i will|i can|will do|on it|ako na|ako bahala|ako nalang|ako na lang|gagawin ko|gawin ko|i got it|sige ako|noted,? i)\b",
    re.I,
)
BROADCAST = re.compile(r"(@everyone|@all\b|\beveryone\b|\ball eb\b|\ball officers\b|\bguys\b)", re.I)


# --- mentions -----------------------------------------------------------------------------------


@dataclass
class Profile:
    """Who you are, as the rules need it."""

    names: list = field(default_factory=list)  # "Aldrich", "Aldrich Lotas", nicknames
    titles: dict = field(default_factory=dict)  # org -> ["DCCO", "Deputy CCO"...]
    committees: dict = field(default_factory=dict)  # org -> ["Creatives", "Publicity"...]
    above: dict = field(default_factory=dict)  # org -> ["Sam", "Isabelle Reyes", "Ystiphen"...]


def _words_rx(words: list):
    words = sorted({w.strip() for w in words if w and w.strip()}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<![\w&])(" + "|".join(re.escape(w) for w in words) + r")(?![\w&])", re.I)


def mention(text: str, org, prof: Profile):
    """(strength, reason) for how strongly a message is about you, or (0, "")."""
    if not text:
        return 0, ""
    rx = _words_rx(prof.names)
    if rx:
        m = rx.search(text)
        if m:
            return 3, f"names you ({m.group(1)})"
    titles = prof.titles.get(org, []) if org else [t for v in prof.titles.values() for t in v]
    rx = _words_rx(titles)
    if rx:
        m = rx.search(text)
        if m:
            return 3, f"your role ({m.group(1)})"
    comms = prof.committees.get(org, []) if org else []
    rx = _words_rx(comms)
    if rx:
        m = rx.search(text)
        if m:
            return 2, f"your committee ({m.group(1)})"
    if BROADCAST.search(text):
        return 1, "everyone"
    return 0, ""


def is_above(author, org, prof: Profile) -> bool:
    if not author:
        return False
    rx = _words_rx(prof.above.get(org, []) if org else [a for v in prof.above.values() for a in v])
    return bool(rx and rx.search(author))


# --- putting it together ------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str  # deadline | meeting | task
    due: object  # datetime or None
    has_time: bool
    confidence: float
    reasons: list


def clean_title(text: str, n: int = 90) -> str:
    t = re.sub(r"https?://\S+", "[link]", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def analyse(text: str, ts: dt.datetime, author, is_me: bool, org, prof: Profile, strength: int = None):
    """What a single message asks of you, if anything (None when nothing actionable)."""
    if not text or text.startswith("(sticker"):
        return None
    hit = find_date(text, ts)
    if hit and hit.when < ts - dt.timedelta(hours=12):
        hit = None  # refers to the past
    if strength is None:
        strength, _ = mention(text, org, prof)
    meeting, deadline, request = bool(MEETING.search(text)), bool(DEADLINE.search(text)), bool(REQUEST.search(text))
    reasons = []
    if is_me:
        if MY_COMMIT.search(text) and (hit or request):
            reasons.append("you committed to it")
            return Finding("task", hit.when if hit else None, bool(hit and hit.has_time), 0.6, reasons)
        if hit and (meeting or deadline):
            # You announced a date to the group: you'll want to track it.
            kind = "meeting" if meeting and not deadline else "deadline"
            conf = 0.65 + (0.1 if BROADCAST.search(text) else 0)
            return Finding(kind, hit.when, hit.has_time, round(conf, 2), [f"you announced {hit.text}"])
        return None
    above = is_above(author, org, prof)
    if above:
        reasons.append(f"from {author}")
    if strength >= 2:
        reasons.append("mentions you" if strength == 3 else "mentions your committee")
    # "pubmat for the GA, deadline Thu" is work to deliver, not a meeting to attend.
    if hit and meeting and not (request and (deadline or strength >= 2)):
        conf = 0.5 + (0.2 if above else 0) + (0.15 if strength >= 2 else 0) + (0.1 if hit.has_time and PLACE.search(text) else 0)
        reasons.insert(0, f"meeting {hit.text}")
        return Finding("meeting", hit.when, hit.has_time, round(min(conf, 0.95), 2), reasons)
    if hit and (deadline or request):
        kind = "task" if strength >= 2 else "deadline"
        conf = 0.4 + (0.3 if strength >= 2 else 0) + (0.15 if above else 0) + (0.1 if deadline else 0)
        reasons.insert(0, f"due {hit.text}")
        return Finding(kind, hit.when, hit.has_time, round(min(conf, 0.95), 2), reasons)
    if strength == 3 and request:
        reasons.insert(0, "asks you for something")
        return Finding("task", None, False, 0.5 + (0.15 if above else 0), reasons)
    return None


CAPTURE_TAG = re.compile(r"(?<!\w)#([a-z0-9][\w-]*)", re.I)


def capture(text: str, ts: dt.datetime, orgs=None):
    """Parse a note you sent yourself: kind (capture or win), title, due, hashtags.
    Hashtags naming an org (#ecg) are taken out of the title; others (#school) stay in it."""
    raw = (text or "").strip()
    kind = "capture"
    m = re.match(r"^(/win|win:|win -)\s*", raw, re.I)
    if m:
        kind, raw = "win", raw[m.end() :]
    tags = [t.lower() for t in CAPTURE_TAG.findall(raw)]
    hit = find_date(raw, ts) if kind == "capture" else None
    return {
        "kind": kind,
        "title": clean_title(
            CAPTURE_TAG.sub(lambda m: "" if orgs is None or m.group(1).lower() in orgs else m.group(0), raw).strip() or raw, 140
        ),
        "due": hit.when if hit else None,
        "has_time": bool(hit and hit.has_time),
        "tags": tags,
    }
