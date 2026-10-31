// Injected into facebook.com/messages to watch the left-hand chat list without opening any chat.
// Installs window.__hsw with:
//   drain()  what changed since the last call: { rows, changes, mutations }
//     rows       every mounted chat, top of the list first: { id, sig, lines, weight, rank }
//     changes    chats whose row changed: { id, at, before, after } (at = ms since epoch)
//     mutations  DOM changes inside chat rows since the last call
// Row text is never returned, only a hash of it (sig), so the recording holds no message bodies.
// sig leaves out what changes by itself ("26m" labels update about once a minute, "Active now" comes and
// goes, "Unread message:" clears when you read on your phone), so it moves only when the preview does.
// weight is the heaviest font weight in the row: 600 or more looks like unread, and rows turning bold
// lined up with real messages, but a chat that's already bold doesn't flip again on its next message.
// Relies only on links to /messages/t/<id> or /messages/e2ee/t/<id>, as discover.js does.
(() => {
  if (window.__hsw) return;

  const LINK = 'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]';
  const sigs = new Map();
  let changes = [];
  let mutations = 0;

  function hash(text) {
    let h = 5381;
    for (let i = 0; i < text.length; i++) h = (h * 33 + text.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }

  const SPAN = '(?:\\d{1,3}\\s?(?:m|h|d|w|y|mins?|hrs?|hours?|days?|wks?|weeks?|yrs?|years?)(?:\\s+ago)?|just now|now|yesterday)';
  const DAY = '(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\s+\\d{1,2}(?:,\\s*\\d{4})?';
  const CLOCK = '\\d{1,2}[:.]\\d{2}(?:\\s*[ap]\\.?m\\.?)?';
  const WHEN = `(?:${SPAN}|${DAY}|${CLOCK})`;
  const ALONE = new RegExp(`^${WHEN}$`, 'i');
  const AFTER_DOT = new RegExp(`\\s*[·•]\\s*${WHEN}$`, 'i');
  const STATUS = /^(?:active(?:\s+now|\s+\d.*ago)?|unread message:?)$/i;

  // The row's text without its time label or anything else that changes while the chat stays the same.
  function stable(text) {
    return text
      .split('\n')
      .map((line) => line.replace(/^unread message:\s*/i, '').replace(AFTER_DOT, '').trim())
      .filter((line) => line && !/^[·•]$/.test(line) && !ALONE.test(line) && !STATUS.test(line))
      .join('\n');
  }

  function read() {
    const rows = [];
    for (const a of document.querySelectorAll(LINK)) {
      if (a.closest('[role="log"]')) continue;
      const m = (a.getAttribute('href') || '').match(/\/messages\/(?:e2ee\/)?t\/([^/?#]+)/);
      if (!m) continue;
      const text = a.innerText || a.getAttribute('aria-label') || '';
      const lines = text.split('\n').filter((s) => s.trim()).length;
      let weight = 0;
      for (const el of a.querySelectorAll('span')) {
        weight = Math.max(weight, parseInt(getComputedStyle(el).fontWeight, 10) || 0);
      }
      rows.push({ id: m[1], sig: hash(stable(text)), lines, weight, rank: rows.length });
    }
    return rows;
  }

  function compare() {
    const rows = read();
    for (const r of rows) {
      const before = sigs.get(r.id);
      if (before !== undefined && before !== r.sig) {
        changes.push({ id: r.id, at: Date.now(), before, after: r.sig });
      }
      sigs.set(r.id, r.sig);
    }
    return rows;
  }

  compare();
  new MutationObserver((records) => {
    const inRow = records.filter((r) => {
      const el = r.target.nodeType === 1 ? r.target : r.target.parentElement;
      return el && el.closest(LINK) && !el.closest('[role="log"]');
    });
    if (!inRow.length) return;
    mutations += inRow.length;
    compare();
  }).observe(document.documentElement, { subtree: true, childList: true, characterData: true });

  window.__hsw = {
    drain() {
      const rows = compare();
      const out = { rows, changes, mutations };
      changes = [];
      mutations = 0;
      return out;
    },
  };
})();
