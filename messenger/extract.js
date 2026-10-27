// Injected into an open Messenger conversation on facebook.com/messages/t/... (2026 markup).
// Installs window.__hsx with:
//   step()      tag every mounted message / date break in DOM order and (re-)extract it
//   scrollUp()  scroll the message list to the top so Messenger loads older history
//   dump()      every item seen so far, oldest first
// Structure relied on (class names are obfuscated, so only ARIA and data-scope):
//   [role="log"]                         the conversation
//   [data-scope="date_break"]            time separator ("7:56 PM", "Yesterday at ...", ...)
//   [role="article"]                     one message, containing
//     [aria-roledescription="message"]   aria-label "At 8:15 PM, Patrick: text"
//     [role="button"] aria-label         "Enter, Message sent 8:15 PM by Patrick: text"
//     h3                                 sender's short name (first message of a cluster)
//     img in [role="button"]             sender avatar, alt = full name
//     a[href*="/messenger_media/"] img   photos (href carries attachment_id and message_id)
//     video                              videos
//     [role="button"][aria-label*="reaction"]  reaction pill
// Each item also keeps `raw` (innerText) so nothing is lost when a heuristic misses.
(() => {
  if (window.__hsx) return;

  const S = { top: 0, bottom: 0, rows: new Map(), scroller: null };
  const REPLY_RE = /replied to|replying to|original message/i;
  const SENT_RE = /^Enter, Message sent (.+?) by (.+?)(?:: ([\s\S]*))?$/;
  const AT_RE = /^At (.+?), (.+?)(?:: ([\s\S]*))?$/;

  const log = () => document.querySelector('[role="log"]');

  function scrollable(el) {
    const cs = getComputedStyle(el);
    return /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 10;
  }

  function scroller(root) {
    if (S.scroller && S.scroller.isConnected) return S.scroller;
    let best = null;
    for (const d of root.querySelectorAll('div')) {
      if (scrollable(d) && (!best || d.scrollHeight > best.scrollHeight)) best = d;
    }
    if (!best) {
      for (let el = root; el && el !== document.body; el = el.parentElement) {
        if (scrollable(el)) {
          best = el;
          break;
        }
      }
    }
    return (S.scroller = best);
  }

  function cleanUrl(href) {
    try {
      const u = new URL(href, location.href);
      if (/(^|\.)facebook\.com$|(^|\.)messenger\.com$/.test(u.hostname) && u.pathname === '/l.php') {
        return u.searchParams.get('u') || u.href;
      }
      return u.href;
    } catch (e) {
      return href;
    }
  }

  function expandSeeMore(el) {
    for (const b of el.querySelectorAll('[role="button"]')) {
      if (/^see more$/i.test((b.innerText || '').trim())) b.click();
    }
  }

  function textBlocks(msg) {
    return [...msg.querySelectorAll('[dir="auto"]')]
      .filter((d) => !d.closest('h3') && !(d.parentElement && d.parentElement.closest('[dir="auto"]')))
      .map((d) => {
        // Emoji are <img alt="😭">; put them back into the text.
        const clone = d.cloneNode(true);
        for (const img of clone.querySelectorAll('img[alt]')) img.replaceWith(img.alt);
        return clone.innerText.trim();
      })
      .filter(Boolean);
  }

  function extractBreak(el) {
    return { kind: 'time', label: (el.innerText || '').trim(), raw: (el.innerText || '').trim() };
  }

  function extractArticle(art) {
    const msg = art.querySelector('[aria-roledescription="message"]') || art;
    const raw = (art.innerText || '').trim();
    let when = '';
    let sender = '';
    let labelText = '';
    const sentBtn = [...art.querySelectorAll('[role="button"][aria-label]')].find((b) =>
      SENT_RE.test(b.getAttribute('aria-label'))
    );
    const m =
      (sentBtn && sentBtn.getAttribute('aria-label').match(SENT_RE)) ||
      (msg.getAttribute('aria-label') || '').match(AT_RE);
    if (m) {
      when = m[1].replace(/ /g, ' ').trim();
      sender = m[2].trim();
      labelText = (m[3] || '').trim();
    }

    const h3 = msg.querySelector('h3');
    const heading = h3 ? h3.innerText.trim() : '';

    let avatar = '';
    const images = [];
    for (const img of msg.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      const alt = img.alt || '';
      if (!/scontent|fbcdn/.test(src) || /static\.xx\.fbcdn/.test(src) || /^seen by/i.test(alt)) continue;
      const link = img.closest('a[href]');
      if (!link && img.closest('[role="button"]') && !img.closest('[role="button"][aria-label]')) {
        avatar = avatar || alt.trim();
        continue;
      }
      if (img.closest('[role="button"][aria-label*="reaction"]')) continue;
      const href = link ? link.getAttribute('href') : '';
      images.push({ url: src, alt, attachment_id: (href.match(/attachment_id=(\d+)/) || [])[1] || '' });
    }
    const videos = [...msg.querySelectorAll('video')].map((v) => ({ url: v.currentSrc || v.src || '' }));
    const audio = [...msg.querySelectorAll('audio')].map((a) => ({ url: a.currentSrc || a.src || '' }));

    let mid = '';
    const files = [];
    const links = [];
    for (const a of msg.querySelectorAll('a[href]')) {
      const rawHref = a.getAttribute('href');
      const midMatch = rawHref.match(/message_id=([^&]+)/);
      if (midMatch && !mid) mid = decodeURIComponent(midMatch[1]);
      if (/\/messenger_media\//.test(rawHref) && a.querySelector('img')) continue; // photo, handled above
      const href = cleanUrl(a.href);
      const name = (a.innerText || a.getAttribute('aria-label') || '').trim();
      if (/cdn\.fbsbx\.com|\/messenger_media\/|\/attachments?\//.test(href) || a.hasAttribute('download')) {
        files.push({ url: href, name });
      } else if (/^https?:/.test(href) && !/^https?:\/\/(www\.)?(facebook|messenger)\.com\//.test(href)) {
        links.push(href);
      }
    }

    const reactions = [...msg.querySelectorAll('[role="button"][aria-label]')]
      .map((b) => b.getAttribute('aria-label'))
      .filter((l) => /reaction/i.test(l));

    let texts = textBlocks(msg).filter((t) => t !== heading && !REPLY_RE.test(t));
    let replyHeader = '';
    let replyQuote = '';
    const headerLine = raw.split('\n').find((l) => REPLY_RE.test(l));
    if (headerLine) {
      replyHeader = headerLine.trim();
      if (texts.length >= 2) replyQuote = texts.shift();
    }
    let text = texts.join('\n');
    if (!text && labelText && !images.length && !videos.length) text = labelText;

    return {
      kind: 'message',
      mid,
      when,
      sender,
      heading,
      avatar,
      text,
      label_text: labelText,
      reply_header: replyHeader,
      reply_quote: replyQuote,
      reactions,
      edited: /(^|\n)edited($|\n)/i.test(raw),
      images,
      videos,
      audio,
      files,
      links,
      raw,
    };
  }

  function items(root) {
    return [...root.querySelectorAll('[role="article"], [data-scope="date_break"]')].filter(
      (el) => !el.parentElement.closest('[role="article"]')
    );
  }

  function step() {
    const root = log();
    if (!root) return { ok: false, reason: 'no [role="log"] message list found' };
    const els = items(root);
    let added = 0;
    const recs = new Map(
      els.map((r) => {
        if (r.matches('[data-scope="date_break"]')) return [r, extractBreak(r)];
        expandSeeMore(r);
        return [r, extractArticle(r)];
      })
    );

    // Messenger rebuilds rows as it scrolls, and a rebuilt row has lost its tag. Without this it was
    // stored a second time (whole screens repeated ~20 rows later in the export). So an untagged row
    // first takes back the tag of an identical row collected earlier that isn't on screen now; two
    // genuinely identical messages are on screen together, so they never claim each other.
    const mounted = new Set(els.map((r) => r.dataset.hsx).filter(Boolean));
    let byRaw = null;
    const claim = (r, near, maxDist) => {
      const raw = recs.get(r).raw;
      if (!raw) return null;
      if (!byRaw) {
        byRaw = new Map();
        for (const [seq, rec] of S.rows) {
          if (rec.raw) (byRaw.get(rec.raw) || byRaw.set(rec.raw, []).get(rec.raw)).push(seq);
        }
      }
      let best = null;
      for (const seq of byRaw.get(raw) || []) {
        const d = Math.abs(parseFloat(seq) - near);
        if (!mounted.has(seq) && d <= maxDist && (!best || d < best.d)) best = { seq, d };
      }
      if (!best) return null;
      mounted.add(best.seq);
      return best.seq;
    };

    // Untagged items above the first tagged one: rebuilt rows (claimed bottom-up from the tagged
    // neighbour), then older history, numbered downwards from S.top.
    const firstTagged = els.findIndex((r) => r.dataset.hsx);
    const above = firstTagged === -1 ? els : els.slice(0, firstTagged);
    let near = firstTagged === -1 ? S.top : parseFloat(els[firstTagged].dataset.hsx);
    let maxDist = firstTagged === -1 ? Infinity : 40;
    let k = above.length - 1;
    for (; k >= 0; k--) {
      const seq = claim(above[k], near, maxDist);
      if (seq === null) break;
      above[k].dataset.hsx = seq;
      near = parseFloat(seq);
      maxDist = 3;
    }
    const fresh = above.slice(0, k + 1);
    fresh.forEach((r, i) => {
      r.dataset.hsx = String(S.top - fresh.length + i);
    });
    S.top -= fresh.length;
    added += fresh.length;

    // Anything else untagged was rebuilt mid-list or arrived at the bottom.
    let prev = null;
    for (const r of els) {
      if (!r.dataset.hsx) {
        const seq = prev === null ? null : claim(r, prev, 3);
        if (seq !== null) {
          r.dataset.hsx = seq;
        } else {
          r.dataset.hsx = prev === null ? String(++S.bottom) : String(prev + 0.001);
          added += 1;
        }
      }
      prev = parseFloat(r.dataset.hsx);
      if (prev > S.bottom) S.bottom = Math.ceil(prev);
    }

    for (const r of els) {
      const rec = recs.get(r);
      const old = S.rows.get(r.dataset.hsx);
      // Keep the richer snapshot (lazy images may not have loaded the first time).
      if (!old || JSON.stringify(rec).length >= JSON.stringify(old).length) S.rows.set(r.dataset.hsx, rec);
    }
    const sc = scroller(root);
    return {
      ok: true,
      mounted: els.length,
      added,
      total: S.rows.size,
      scrollTop: sc ? sc.scrollTop : null,
      scrollHeight: sc ? sc.scrollHeight : null,
    };
  }

  function scrollUp() {
    const root = log();
    const sc = root && scroller(root);
    if (!sc) return false;
    sc.scrollTop = 0;
    sc.dispatchEvent(new Event('scroll'));
    return true;
  }

  function dump() {
    return [...S.rows.entries()]
      .map(([seq, rec]) => Object.assign({ seq: parseFloat(seq) }, rec))
      .sort((a, b) => a.seq - b.seq);
  }

  function title() {
    const root = log();
    const label = (root && root.getAttribute('aria-label')) || '';
    const m = label.match(/titled (.+)$/) || label.match(/with (.+)$/);
    return m ? m[1].trim() : document.title;
  }

  window.__hsx = { step, scrollUp, dump, title };
})();
