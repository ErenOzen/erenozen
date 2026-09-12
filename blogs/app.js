/* Programming Blog Finder -- main thread.
 * Owns UI, facet state and URL sync. All matching happens in search-worker.js.
 */
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const PAGE = 40;
const DAY0 = Date.UTC(2006, 0, 1) / 86400000;  // matches search-worker.js
const state = {
  q: "",
  mode: "posts",
  sort: "relevance",
  topics: new Set(),
  kinds: new Set(),
  sources: new Set(),
  hideNews: true,
  since: 0,          // years back; 0 = any time
  hideDead: false,
  blog: -1,
  limit: PAGE,
  seq: 0,
  lastSeq: -1,
  meta: null,
  blogs: [],
  ready: false,
  prevMode: null,
  loadingCorpus: true,
  loaded: 0,
  nPosts: 0,
};

const worker = new Worker("search-worker.js");
worker.postMessage({ type: "load", base: "data/" });

/* ---------- boot ---------- */

worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === "ready") {
    state.meta = m.meta;
    state.blogs = m.blogs;
    state.ready = true;
    onIndexReady();
  } else if (m.type === "results") {
    if (m.seq < state.lastSeq) return; // a newer query already landed
    state.lastSeq = m.seq;
    render(m);
  } else if (m.type === "progress") {
    // The corpus is still arriving in score order, so the current answer is a
    // true prefix of the final one. Re-run so it fills in, and say so: a count
    // that climbs on its own is otherwise indistinguishable from a bug.
    state.loaded = m.loaded;
    state.nPosts = m.total;
    showLoadNote();
    if (state.ready) rerunKeepingCursor();
  } else if (m.type === "titles-complete") {
    state.loaded = m.loaded;
    state.nPosts = m.total;
    state.loadingCorpus = false;
    showLoadNote();
    if (state.ready) rerunKeepingCursor();
  } else if (m.type === "paths-ready" || m.type === "hn-ready") {
    // Re-run so rows pick up their exact article URL, then their HN thread.
    if (state.ready) rerunKeepingCursor();
  } else if (m.type === "similar") {
    if (m.blog === state.blog) renderSimilar(m.rows);
  } else if (m.type === "export") {
    downloadOPML(m.rows, m.total);
  } else if (m.type === "error") {
    $("#tagline").textContent = "Index failed to load — " + m.message;
    const status = $("#status");
    status.textContent = "";
    const retry = el("button", "more", "Retry");
    retry.addEventListener("click", () => {
      $("#tagline").textContent = "Loading index…";
      status.textContent = "";
      worker.postMessage({ type: "load", base: "data/" });
    });
    status.appendChild(retry);
  }
};

worker.onerror = (err) => {
  $("#status").textContent = "Search failed to start: " + err.message;
};

function onIndexReady() {
  const m = state.meta;
  $("#tagline").innerHTML =
    `<strong>${m.n_posts.toLocaleString()}</strong> posts from ` +
    `<strong>${m.n_blogs.toLocaleString()}</strong> blogs, ranked by how Hacker News ` +
    `actually received them. Newsroom and vendor posts are hidden by default.`;
  $("#stat-stories").textContent = m.n_stories_scanned.toLocaleString();
  $("#stat-built").textContent =
    "index built " + new Date(m.built * 1000).toISOString().slice(0, 10);

  syncSegs();   // stamp the initial aria-pressed values
  buildChips($("#topics"), m.topics, state.topics, "topic");
  // Collapsed on phones, where the taxonomy alone pushed every result below
  // the fold. Any active topic filter forces it open so the state stays visible.
  const wrap = $("#topic-wrap");
  if (window.matchMedia("(max-width: 560px)").matches && !state.topics.size) {
    wrap.open = false;
  }
  buildChips($("#kinds"), m.kinds, state.kinds, "kind");
  // Only sources that actually have blogs. "Institution" is declared in the
  // taxonomy and classifies nothing, and a chip that can only ever return
  // zero results is worse than no chip.
  const srcCount = new Map();
  for (const b of state.blogs) srcCount.set(b.s, (srcCount.get(b.s) || 0) + 1);
  buildChips($("#sources"), m.sources, state.sources, "source",
             (i) => (srcCount.get(i) || 0) > 0);

  // The longest date filter is relative (8 years back), so its label has to be
  // computed. Shipped as "Since 2018", which would have quietly meant "since
  // 2019" every January while still saying 2018.
  const far = document.querySelector('[data-since="8"]');
  if (far) far.textContent = "Since " + (new Date().getUTCFullYear() - 8);

  document.querySelectorAll("#suggests button").forEach((b) => {
    b.addEventListener("click", () => {
      state.q = b.dataset.q;
      $("#q").value = state.q;
      state.limit = PAGE;
      run(true);
      $("#q").focus();
    });
  });

  const q = $("#q");
  q.disabled = false;
  readURL();
  q.value = state.q;
  q.focus();
  run();
}

/* Outside the aria-live status region on purpose: this ticks several times a
 * second while the corpus streams in, and announcing each tick would bury the
 * result count a screen-reader user actually asked for. */
function showLoadNote() {
  const n = $("#load-note");
  if (!state.loadingCorpus || state.loaded >= state.nPosts) {
    n.hidden = true;
    return;
  }
  n.hidden = false;
  n.textContent = `loading ${Math.round((100 * state.loaded) / state.nPosts)}% of the corpus`;
}

function buildChips(host, items, set, kind, include) {
  host.textContent = "";
  items.forEach((it, i) => {
    if (include && !include(i)) return;
    const b = el("button", "chip", it.name);
    b.setAttribute("aria-pressed", "false");
    b.dataset.idx = i;
    b.dataset.kind = kind;
    b.addEventListener("click", () => {
      set.has(i) ? set.delete(i) : set.add(i);
      b.setAttribute("aria-pressed", String(set.has(i)));
      if (kind === "source") syncNewsToggle();
      state.limit = PAGE;
      run();
    });
    host.appendChild(b);
  });
}

/* ---------- query ---------- */

function filters() {
  let topicMask = 0;
  for (const i of state.topics) topicMask |= 1 << i;
  let kindMask = 0;
  for (const i of state.kinds) kindMask |= 1 << i;
  let sourceMask = 0;
  for (const i of state.sources) sourceMask |= 1 << i;
  // The worker stores dates as day offsets from 2006-01-01, so convert once
  // here rather than per post inside the filter loop.
  let sinceDay = 0, sinceYear = 0;
  if (state.since) {
    const cut = new Date();
    cut.setUTCFullYear(cut.getUTCFullYear() - state.since);
    sinceDay = Math.floor(cut.getTime() / 86400000) - DAY0;
    sinceYear = cut.getUTCFullYear();
  }
  return {
    topicMask,
    kindMask,
    blogId: state.blog,
    hideNews: state.hideNews,
    sourceMask,
    sinceDay,
    sinceYear,
    hideDead: state.hideDead,
    hiddenSourceMask: state.meta ? state.meta.hidden_source_mask : 0,
  };
}

/* A re-run the reader did not ask for.
 *
 * render() empties the list and calls selectRow(-1), which moves real DOM
 * focus. Streaming fires one of these every 400ms for the whole download, and
 * the entire point of streaming is that the reader is already reading: arrowing
 * into the results had their focused row deleted from under them, focus dropped
 * to <body>, and the cursor reset, three times a second. render() already knows
 * how to restore a row; nothing was telling it to. */
function rerunKeepingCursor() {
  if (sel >= 0) pendingFocusRow = sel;
  run(true);
}

let timer = null;
function run(immediate) {
  clearTimeout(timer);
  const go = () => {
    worker.postMessage({
      type: "query",
      seq: ++state.seq,
      q: state.q.trim(),
      mode: state.mode,
      filters: filters(),
      sort: state.sort,
      limit: state.limit,
    });
    writeURL();
  };
  // Narrowed keystrokes are ~1.5ms, so a short debounce keeps typing smooth
  // without the UI feeling laggy.
  immediate ? go() : (timer = setTimeout(go, 45));
}

/* ---------- render ---------- */

function highlight(text, q) {
  const frag = document.createDocumentFragment();
  const terms = q.trim().toLowerCase().split(/\s+/).filter((t) => t.length > 1);
  if (!terms.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  const lower = text.toLowerCase();
  const marks = [];
  for (const t of terms) {
    let from = 0, at;
    while ((at = lower.indexOf(t, from)) !== -1) {
      marks.push([at, at + t.length]);
      from = at + t.length;
    }
  }
  if (!marks.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  marks.sort((a, b) => a[0] - b[0]);
  const merged = [marks[0]];
  for (const m of marks.slice(1)) {
    const last = merged[merged.length - 1];
    if (m[0] <= last[1]) last[1] = Math.max(last[1], m[1]);
    else merged.push(m);
  }
  let cur = 0;
  for (const [s, e] of merged) {
    if (s > cur) frag.appendChild(document.createTextNode(text.slice(cur, s)));
    frag.appendChild(el("mark", null, text.slice(s, e)));
    cur = e;
  }
  if (cur < text.length) frag.appendChild(document.createTextNode(text.slice(cur)));
  return frag;
}

function render(m) {
  // Dispatch on the mode the worker echoed, not current state: clicking a mode
  // tab mutates state.mode synchronously while a query is still in flight, and
  // the seq guard only drops results OLDER than one already rendered.
  const mode = m.mode || state.mode;
  const list = $("#results");
  list.textContent = "";
  const status = $("#status");
  const anyFilter =
    state.topics.size || state.kinds.size || state.sources.size || !state.hideNews ||
    state.since || state.hideDead || state.q.trim() || state.blog >= 0;
  $("#reset").hidden = !anyFilter;

  if (!m.rows.length) {
    $("#suggests").hidden = !!state.q.trim() || state.blog >= 0;
    renderPin();
    // Clearing a polite live region announces nothing, so the last count the
    // user heard stays their mental model. Say the failure out loud.
    const qq = state.q.trim();
    status.textContent = qq
      ? `No matches for \u201c${qq}\u201d`
      : "Nothing matches these filters";
    $("#status-ms").textContent = "";
    list.appendChild(noResults());
    $("#more").hidden = true;
    $("#opml").hidden = true;
    selectRow(-1);
    return;
  }

  const ORDER = {
    relevance: state.q.trim() ? "best match" : "top ranked",
    points: "most upvoted",
    date: "newest first",
    oldest: "oldest first",
  };
  // Timing lives OUTSIDE the live region: it changes on every keystroke and
  // would queue an announcement per character that differs only in the ms.
  // Silence the live region while the corpus streams. The count legitimately
  // changes on every one of those 400ms ticks, and announcing each one buries
  // the answer the reader asked for -- the same reason the load note was kept
  // out of this region in the first place. The settled count is announced once
  // when streaming ends and the region goes polite again.
  status.setAttribute("aria-live", state.loadingCorpus ? "off" : "polite");
  status.innerHTML =
    `<span class="hl">${m.total.toLocaleString()}</span> ${mode === "blogs" ? "blogs" : "posts"}` +
    ` · ${ORDER[state.sort]}`;
  $("#status-ms").textContent = m.ms.toFixed(1) + "ms";

  $("#suggests").hidden = !!state.q.trim() || state.blog >= 0;
  renderPin();

  const frag = document.createDocumentFragment();
  for (const r of m.rows) {
    frag.appendChild(mode === "blogs" ? blogRow(r) : postRow(r));
  }
  list.appendChild(frag);
  $("#more").hidden = m.rows.length >= m.total;
  $("#opml").hidden = mode !== "blogs";
  if (pendingFocusRow >= 0) {
    selectRow(Math.min(pendingFocusRow, m.rows.length - 1));
    pendingFocusRow = -1;
  } else {
    selectRow(-1);
  }
}

function postRow(r) {
  const b = state.blogs[r.b] || { n: "?", h: "#" };
  const li = el("li", "post-li");
  const a = el("a", "row");
  a.href = r.u ? b.h.replace(/\/$/, "") + r.u : b.h;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.tabIndex = -1;   // reachable by arrow keys; not a tab stop

  const t = el("div", "r-title");
  t.appendChild(highlight(r.t, state.q));
  a.appendChild(t);

  const meta = el("div", "r-meta");
  const bits = [];
  bits.push(el("span", "r-blog", b.n));

  // Feed-sourced posts never reached HN, so they have no score. Printing
  // "▲ 0" would read as "nobody liked this" rather than "never submitted".
  if (r.fd) {
    const src = el("span", "r-feed", "from feed");
    src.title = "Found in the blog's own feed — not submitted to Hacker News";
    bits.push(src);
  } else {
    const pts = el("span", "r-pts", "▲ " + r.p);
    pts.setAttribute("aria-label", `${r.p} points on Hacker News`);
    bits.push(pts);
  }

  const year = new Date(r.d).getUTCFullYear();
  const yr = el("span", "r-year", String(year));
  yr.setAttribute("aria-label", `posted ${year}`);
  bits.push(yr);

  // Only label the kind when a title rule actually fired. Roughly three
  // quarters of posts fall back to their publisher's default, and stamping
  // "Release" on every one of those presents a guess as a fact -- it read as
  // noise on almost every row.
  const kind = state.meta.kinds[r.k];
  if (kind && r.kr) bits.push(el("span", "r-tag", kind.name));
  const src = state.meta.sources[r.s];
  if (src && src.hidden_by_default) bits.push(el("span", "r-tag", src.name));
  if (r.dead) {
    const d = el("span", "r-tag r-dead", "link may be dead");
    d.title = "This URL did not respond when last checked";
    bits.push(d);
  }
  bits.forEach((node, i) => {
    if (i) meta.appendChild(el("span", "dot", " · "));
    meta.appendChild(node);
  });
  a.appendChild(meta);
  li.appendChild(a);

  if (r.dead) {
    const arc = el("a", "hn-link arc-link");
    arc.href = "https://web.archive.org/web/" + a.href;
    arc.target = "_blank";
    arc.rel = "noopener noreferrer";
    arc.textContent = "archived copy";
    li.appendChild(arc);
  }

  if (r.h) {
    const hn = el("a", "hn-link");
    hn.href = "https://news.ycombinator.com/item?id=" + r.h;
    hn.target = "_blank";
    hn.rel = "noopener noreferrer";
    hn.textContent = "HN discussion";
    hn.title = "Read the Hacker News thread";
    li.appendChild(hn);
  }
  return li;
}

function blogRow(r) {
  const li = el("li");
  const a = el("a", "row");
  a.href = r.h;
  a.target = "_blank";
  a.rel = "noopener noreferrer";

  const t = el("div", "r-title");
  t.appendChild(highlight(r.n, state.q));
  a.appendChild(t);

  if (r.o) a.appendChild(el("div", "r-desc", r.o));

  const meta = el("div", "r-meta");
  meta.appendChild(el("span", "r-pts", `${r.c} posts · median ▲${r.m}`));
  const src = state.meta.sources[r.s];
  if (src) meta.appendChild(el("span", "r-tag", src.name));
  // Only for blogs that have gone quiet. Stamping "last active 2026" on the
  // 4,218 current ones is noise; "last active 2016" is the single most useful
  // thing to know before subscribing to one.
  const thisYear = new Date().getUTCFullYear();
  if (r.l && r.l < thisYear - 1) {
    const dormant = el("span", "r-tag r-dormant", `last active ${r.l}`);
    dormant.title = "No posts seen since then, on Hacker News or in its feed";
    meta.appendChild(dormant);
  }
  for (const ti of state.meta.topics.keys()) {
    if ((r.tm >> ti) & 1) meta.appendChild(el("span", "r-tag", state.meta.topics[ti].name));
  }
  a.appendChild(meta);
  li.appendChild(a);

  // A flex container rather than two absolutely-positioned links: "5 posts →"
  // and "1,284 posts →" are very different widths, so any fixed right offset
  // for a second link would overlap on some rows and float on others.
  const acts = el("div", "row-actions");
  if (r.f) acts.appendChild(feedLink(r.f, r.n));
  const open = el("button", "hn-link blog-open", `${r.c} posts →`);
  open.title = `Show ${r.n}'s posts`;
  open.addEventListener("click", () => pinBlog(r.i));
  acts.appendChild(open);
  li.appendChild(acts);
  return li;
}

/* Finding a blog worth reading and then having no way to follow it was the
 * gap this closes. Only ~60% of indexed blogs expose a discoverable feed, so
 * the link is conditional rather than a dead affordance on every row. */
function feedLink(href, name) {
  const a = el("a", "hn-link feed-link", "RSS");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.title = `Subscribe to ${name}`;
  return a;
}

function pinBlog(idx) {
  if (idx !== state.blog) simRows = null;
  // Remember where the drill-down started. Pinning always switches to posts --
  // that is the point -- but the way out is labelled "all blogs", and it used
  // to leave you in posts mode looking at the whole corpus. The button
  // promised one thing and did another.
  if (state.blog < 0) state.prevMode = state.mode;
  state.blog = idx;
  state.mode = "posts";
  state.q = "";
  $("#q").value = "";
  state.limit = PAGE;
  document.querySelectorAll(".mode-switch button").forEach((x) =>
    x.classList.toggle("active", x.dataset.mode === "posts"),
  );
  run(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderPin() {
  const host = $("#pin");
  host.textContent = "";
  // Drives a phone-only rule that drops the corpus-wide tagline. Once a blog is
  // pinned, "165,458 posts from 10,535 blogs" describes something the reader
  // has explicitly navigated away from, and on a 640px screen it was costing
  // 65px that the blog's own posts needed.
  document.body.classList.toggle("has-pin", state.blog >= 0);
  if (state.blog < 0) {
    host.hidden = true;
    return;
  }
  const b = state.blogs[state.blog];
  if (!b) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const name = el("a", "pin-name", b.n);
  name.href = b.h;
  name.target = "_blank";
  name.rel = "noopener noreferrer";
  host.appendChild(name);
  if (b.o) host.appendChild(el("span", "pin-desc", b.o));
  if (b.f) host.appendChild(feedLink(b.f, b.n));
  const clear = el("button", "pin-clear", "✕ all blogs");
  clear.addEventListener("click", () => {
    state.blog = -1;
    // Arriving straight on ?b=... has no previous mode, and "all blogs" is the
    // sensible destination from a single blog either way.
    state.mode = state.prevMode || "blogs";
    state.prevMode = null;
    document.querySelectorAll(".mode-switch button").forEach((x) =>
      x.classList.toggle("active", x.dataset.mode === state.mode),
    );
    state.limit = PAGE;
    run(true);
  });
  host.appendChild(clear);

  // Asked for once per pinned blog. renderPin runs on every keystroke while a
  // blog is pinned, and re-requesting each time would rebuild nothing but would
  // make the row flicker as it is replaced with an identical one.
  // `|| !simRows` matters: clearing the pin leaves simFor pointing at the blog,
  // so re-pinning the SAME blog hit the cached branch with simRows already
  // nulled and rendered nothing at all, permanently, for that blog.
  if (simFor !== state.blog || !simRows) {
    simFor = state.blog;
    host.appendChild(el("div", "pin-similar", ""));
    worker.postMessage({ type: "similar", blog: state.blog, k: 5, filters: filters() });
  } else if (simRows) {
    host.appendChild(similarRow(simRows));
  }
}

let simFor = -1;
let simRows = null;

function renderSimilar(rows) {
  simRows = rows;
  const slot = $("#pin .pin-similar");
  if (!slot) return;
  slot.replaceWith(similarRow(rows));
}

function similarRow(rows) {
  const box = el("div", "pin-similar");
  if (!rows.length) {
    // Say so rather than leaving a gap: a blog with a short, very specific
    // description genuinely has no near neighbours, and silence reads as a bug.
    box.appendChild(el("span", "pin-similar-label", "No close matches indexed"));
    return box;
  }
  box.appendChild(el("span", "pin-similar-label", "Similar"));
  for (const r of rows) {
    const b = el("button", "chip sim-chip", r.n);
    b.title = r.o || `Show ${r.n}'s posts`;
    b.addEventListener("click", () => pinBlog(r.i));
    box.appendChild(b);
  }
  return box;
}

function noResults() {
  const box = el("div", "empty");
  const q = state.q.trim();
  box.appendChild(el("h3", null, q ? `No matches for “${q}”` : "Nothing matches these filters"));
  const ul = el("ul");
  if (state.topics.size || state.kinds.size)
    ul.appendChild(el("li", null, "Your topic/kind filters may be too narrow — try clearing them."));
  if (state.hideNews)
    ul.appendChild(el("li", null, "Newsrooms and vendor posts are hidden; unticking that widens the corpus a lot."));
  if (q && q.length > 18)
    ul.appendChild(el("li", null, "Long queries match less — try two or three distinctive words."));
  if (q && state.mode === "posts")
    ul.appendChild(el("li", null, "Searching post titles only. Switch to Blogs to find a source by name."));
  if (!ul.childElementCount)
    ul.appendChild(el("li", null, "Try a broader term, or switch between Posts and Blogs."));
  box.appendChild(ul);
  return box;
}

/* ---------- keyboard ---------- */

let sel = -1;

function inSearchUI() {
  const a = document.activeElement;
  return a === document.body || a === $("#q") || $("#results").contains(a);
}

function selectRow(i) {
  const rows = [...document.querySelectorAll("#results > li")];
  rows.forEach((r) => r.classList.remove("sel"));
  sel = i;
  if (i >= 0 && rows[i]) {
    rows[i].classList.add("sel");
    // Move real focus, not just a CSS class: a visual-only cursor is invisible
    // to a screen reader, and native focus makes Enter work without a synthetic
    // handler that could fire while focus sits on some other control.
    const a = rows[i].querySelector("a.row");
    if (a) a.focus({ preventScroll: true });
    rows[i].scrollIntoView({ block: "nearest" });
  }
}

document.addEventListener("keydown", (e) => {
  const rows = document.querySelectorAll("#results > li");
  // WCAG 2.1.4: a bare printable-character shortcut must not fire while the
  // user is typing anywhere. Only claim "/" from the document itself, and never
  // when it carries a modifier (Ctrl+/ and Cmd+/ belong to the browser).
  if (
    e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey &&
    document.activeElement === document.body
  ) {
    e.preventDefault();
    $("#q").focus();
    $("#q").select();
  } else if (e.key === "Escape") {
    if (state.q) {
      state.q = "";
      $("#q").value = "";
      state.limit = PAGE;
      run(true);
    }
    selectRow(-1);
  } else if (e.key === "ArrowDown") {
    // Do not hijack page scrolling unless the user is actually in the search UI.
    if (!rows.length || !inSearchUI()) return;
    e.preventDefault();
    selectRow(Math.min(sel + 1, rows.length - 1));
  } else if (e.key === "ArrowUp") {
    if (!rows.length || !inSearchUI()) return;
    e.preventDefault();
    if (sel <= 0) {
      selectRow(-1);
      $("#q").focus();
    } else selectRow(sel - 1);
  } else if (
    e.key === "Enter" && sel >= 0 && rows[sel] &&
    (document.activeElement === $("#q") || document.activeElement === document.body)
  ) {
    // Without the focus check, Enter on ANY button (sort, theme toggle, a
    // footer link) also re-opened the selected article.
    e.preventDefault();
    const a = rows[sel].querySelector("a");
    if (a) window.open(a.href, "_blank", "noopener");
  }
});

/* ---------- controls ---------- */

$("#q").addEventListener("input", (e) => {
  state.q = e.target.value;
  state.limit = PAGE;
  run();
});

document.querySelectorAll(".mode-switch button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".mode-switch button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.mode = b.dataset.mode;
    // A pin is a posts-mode idea: searchBlogs has no blogId filter, so in Blogs
    // mode the pin header stayed on screen above the full unfiltered blog list,
    // and writeURL round-tripped ?b=...&mode=blogs -- a shared link that
    // restored a pin doing nothing.
    if (state.mode === "blogs" && state.blog >= 0) {
      state.blog = -1;
      state.prevMode = null;
      simRows = null;
      simFor = -1;
    }
    state.limit = PAGE;
    run(true);
  });
});

// Keyed on the data attribute, not the class: Since reuses .sort-switch for
// its looks, and a class-scoped handler would clear Sort's active state and
// set state.sort to undefined on every Since click.
function segGroup(attr, apply) {
  const btns = [...document.querySelectorAll(`[data-${attr}]`)];
  btns.forEach((b) => {
    b.addEventListener("click", () => {
      btns.forEach((x) => {
        x.classList.remove("active");
        x.setAttribute("aria-pressed", "false");
      });
      b.classList.add("active");
      b.setAttribute("aria-pressed", "true");
      apply(b.dataset[attr]);
      state.limit = PAGE;
      run(true);
    });
  });
}
segGroup("sort", (v) => (state.sort = v));
segGroup("since", (v) => (state.since = +v));

$("#hide-news").addEventListener("change", (e) => {
  state.hideNews = e.target.checked;
  // Recommendations are filtered by this too, and the pin survives the toggle,
  // so the cached row would keep showing results from the old setting.
  simFor = -1;
  simRows = null;
  state.limit = PAGE;
  run(true);
});

$("#hide-dead").addEventListener("change", (e) => {
  state.hideDead = e.target.checked;
  state.limit = PAGE;
  run(true);
});

/* ---------- OPML export ---------- */

const OPML_CAP = 300;

$("#opml").addEventListener("click", () => {
  const btn = $("#opml");
  btn.disabled = true;
  btn.textContent = "Building…";
  worker.postMessage({
    type: "export",
    q: state.q.trim(),
    filters: filters(),
    cap: OPML_CAP,
  });
});

const xmlEsc = (v) =>
  String(v || "").replace(/[<>&"']/g, (c) =>
    ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c],
  );

function downloadOPML(rows, total) {
  const btn = $("#opml");
  btn.disabled = false;
  btn.textContent = opmlLabel();
  if (!rows.length) {
    setNotice("None of the matching blogs expose a discoverable feed.");
    return;
  }
  const names = [...state.topics].map((i) => state.meta.topics[i].name);
  const title =
    "Programming blogs" +
    (names.length ? " — " + names.join(", ") : "") +
    (state.q.trim() ? ` — “${state.q.trim()}”` : "");
  const body = rows
    .map(
      (b) =>
        `    <outline type="rss" text="${xmlEsc(b.n)}" title="${xmlEsc(b.n)}"` +
        ` description="${xmlEsc(b.o)}"` +
        ` xmlUrl="${xmlEsc(b.f)}" htmlUrl="${xmlEsc(b.h)}"/>`,
    )
    .join("\n");
  const opml =
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<opml version="2.0">\n  <head>\n' +
    `    <title>${xmlEsc(title)}</title>\n` +
    `    <dateCreated>${new Date().toUTCString()}</dateCreated>\n` +
    "    <ownerName>erenozen.dev/blogs</ownerName>\n" +
    "  </head>\n  <body>\n" +
    body +
    "\n  </body>\n</opml>\n";

  const url = URL.createObjectURL(new Blob([opml], { type: "text/x-opml" }));
  const a = el("a");
  a.href = url;
  a.download =
    "blogs-" +
    (names.length ? names.join("-").toLowerCase().replace(/[^a-z0-9]+/g, "-") + "-" : "") +
    new Date().toISOString().slice(0, 10) +
    ".opml";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoking immediately can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 30000);

  setNotice(
    total > rows.length
      ? `Exported the top ${rows.length} of ${total.toLocaleString()} matching blogs with feeds.`
      : `Exported ${rows.length} blog${rows.length === 1 ? "" : "s"}. Import it in any feed reader.`,
  );
}

function opmlLabel() {
  return "Export as OPML";
}

/* A transient line under the results. Not the aria-live status region: that one
 * belongs to the result count, and overwriting it would make the next search
 * announce a stale sentence about an export. */
let noticeTimer = null;
function setNotice(text) {
  let n = $("#notice");
  if (!n) {
    n = el("div", "notice");
    n.id = "notice";
    n.setAttribute("role", "status");
    $("#results").before(n);
  }
  n.textContent = text;
  n.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => (n.hidden = true), 8000);
}

$("#more").addEventListener("click", () => {
  // The button hides itself once everything is shown, dropping focus to <body>
  // and sending a keyboard user back to the top of the page.
  const resumeAt = document.querySelectorAll("#results > li").length;
  state.limit += PAGE * 2;
  pendingFocusRow = resumeAt;
  run(true);
});

let pendingFocusRow = -1;

$("#reset").addEventListener("click", () => {
  $("#q").focus();
  state.topics.clear();
  state.kinds.clear();
  state.sources.clear();
  syncNewsToggle();
  state.blog = -1;
  state.sort = "relevance";
  state.since = 0;
  state.hideDead = false;
  syncSegs();
  state.hideNews = true;
  state.q = "";
  $("#q").value = "";
  $("#hide-news").checked = true;
  $("#hide-dead").checked = false;
  document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
  state.limit = PAGE;
  run(true);
});

/* The newsroom toggle does nothing while an explicit source is chosen, so say
 * so rather than leaving a live-looking control that has no effect. */
function syncNewsToggle() {
  const box = $("#hide-news");
  const wrap = box.closest(".toggle");
  const off = state.sources.size > 0;
  box.disabled = off;
  wrap.classList.toggle("disabled", off);
  wrap.title = off ? "Not used while a Source filter is active" : "";
}

/* Reflect state back onto both segmented controls. Used by reset and by URL
 * restore, which otherwise leave the buttons showing the previous selection
 * while the results follow the new one. */
function syncSegs() {
  // aria-pressed, not just a class. Sort and Since are the only two controls in
  // the filter panel that conveyed their selection to sighted users alone --
  // the chips beside them have carried aria-pressed all along.
  const mark = (b, on) => {
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  };
  document.querySelectorAll("[data-sort]").forEach((b) =>
    mark(b, b.dataset.sort === state.sort),
  );
  document.querySelectorAll("[data-since]").forEach((b) =>
    mark(b, +b.dataset.since === state.since),
  );
}

/* ---------- URL state ---------- */

function writeURL() {
  const p = new URLSearchParams();
  if (state.q.trim()) p.set("q", state.q.trim());
  if (state.mode !== "posts") p.set("mode", state.mode);
  if (state.sort !== "relevance") p.set("sort", state.sort);
  if (state.topics.size) p.set("t", [...state.topics].join(","));
  if (state.kinds.size) p.set("k", [...state.kinds].join(","));
  if (state.sources.size) p.set("src", [...state.sources].join(","));
  if (!state.hideNews) p.set("news", "1");
  if (state.since) p.set("since", String(state.since));
  if (state.hideDead) p.set("dead", "0");
  if (state.blog >= 0 && state.blogs[state.blog]) p.set("b", state.blogs[state.blog].n);
  const s = p.toString();
  history.replaceState(null, "", s ? "?" + s : location.pathname);
}

function readURL() {
  const p = new URLSearchParams(location.search);
  state.q = p.get("q") || "";
  state.mode = p.get("mode") === "blogs" ? "blogs" : "posts";
  const sortParam = p.get("sort");
  state.sort = ["points", "date", "oldest"].includes(sortParam)
    ? sortParam
    : "relevance";
  state.since = [1, 3, 8].includes(+p.get("since")) ? +p.get("since") : 0;
  syncSegs();
  state.hideNews = p.get("news") !== "1";
  $("#hide-news").checked = state.hideNews;
  state.hideDead = p.get("dead") === "0";
  $("#hide-dead").checked = state.hideDead;
  document.querySelectorAll(".mode-switch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === state.mode);
  });
  const apply = (key, set, host) => {
    (p.get(key) || "")
      .split(",")
      .filter((x) => x !== "")
      .forEach((x) => {
        const i = +x;
        if (!Number.isNaN(i)) {
          set.add(i);
          const c = host.querySelector(`[data-idx="${i}"]`);
          if (c) c.setAttribute("aria-pressed", "true");
        }
      });
  };
  const bkey = p.get("b");
  state.blog = bkey ? state.blogs.findIndex((x) => x.n === bkey) : -1;
  // ?b=...&mode=blogs cannot be honoured -- Blogs mode has no per-blog filter.
  // Older links carry it; show the blog's posts, which is what ?b= meant.
  if (state.blog >= 0 && state.mode === "blogs") state.mode = "posts";
  apply("t", state.topics, $("#topics"));
  apply("k", state.kinds, $("#kinds"));
  apply("src", state.sources, $("#sources"));
  syncNewsToggle();
}

/* ---------- theme (mirrors the portfolio's toggle) ---------- */

const ICON = {
  sun: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line></svg>`,
  moon: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>`,
};
const toggle = el("button", "theme-toggle");
toggle.setAttribute("aria-label", "Toggle dark mode");
if (localStorage.getItem("theme") === "dark") {
  document.documentElement.setAttribute("data-theme", "dark");
  toggle.innerHTML = ICON.moon;
} else {
  toggle.innerHTML = ICON.sun;
}
toggle.addEventListener("click", () => {
  const next =
    (document.documentElement.getAttribute("data-theme") || "light") === "light"
      ? "dark"
      : "light";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  toggle.innerHTML = next === "dark" ? ICON.moon : ICON.sun;
});
document.body.appendChild(toggle);
