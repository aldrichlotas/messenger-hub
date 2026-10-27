// Injected into facebook.com/messages to list the conversations in the left-hand chat list.
// Installs window.__hsd with:
//   collect()     record every chat link currently mounted; returns how many are known
//   scrollDown()  scroll the chat list so Messenger loads more conversations; false if it can't
//   list()        every chat seen so far: { id, url, lines } (the link's visible text lines)
// Relies only on links to /messages/t/<id> or /messages/e2ee/t/<id> outside the open
// conversation ([role="log"]); class names are obfuscated.
(() => {
  if (window.__hsd) return;

  const seen = new Map();
  let sc = null;
  const LINK = 'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]';

  function links() {
    return [...document.querySelectorAll(LINK)].filter((a) => !a.closest('[role="log"]'));
  }

  function collect() {
    for (const a of links()) {
      const href = a.getAttribute('href') || '';
      const m = href.match(/\/messages\/(?:e2ee\/)?t\/([^/?#]+)/);
      if (!m) continue;
      const lines = (a.innerText || '')
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      if (!lines.length && a.getAttribute('aria-label')) lines.push(a.getAttribute('aria-label'));
      if (!lines.length) continue;
      const url = new URL(href, location.href).href.split('?')[0].replace(/\/$/, '');
      // Raw lines: hub.py works out the name (skipping "Active now", community names...).
      if (!seen.has(m[1])) seen.set(m[1], { id: m[1], url, lines: lines.slice(0, 5) });
    }
    return seen.size;
  }

  function scrollable(el) {
    const cs = getComputedStyle(el);
    return /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 10;
  }

  function scroller() {
    if (sc && sc.isConnected) return sc;
    const first = links()[0];
    for (let el = first; el && el !== document.body; el = el.parentElement) {
      if (scrollable(el)) return (sc = el);
    }
    return null;
  }

  function scrollDown() {
    const el = scroller();
    if (!el) return false;
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event('scroll'));
    return true;
  }

  window.__hsd = { collect, scrollDown, list: () => [...seen.values()] };
})();
