/* Search worker.
 *
 * Holds the whole index off the main thread so a cold scan can never drop a
 * frame. Two things here are load-bearing and were verified by benchmark
 * against the real corpus:
 *
 *   1. Narrowing MUST go through uf.filter(hay, needle, prevIdxs). Passing a
 *      prefiltered set to uf.search() does NOT re-test it -- it returns the set
 *      verbatim and then throws inside info(). Measured: cold filter on 400k
 *      titles is ~19ms, narrowing the same query is ~1.5ms.
 *   2. uFuzzy filters a real JS array of strings, not a joined blob, so we hold
 *      the split array and pay the split (~25ms) once at load.
 */
importScripts("vendor/uFuzzy.iife.min.js");

const uf = new uFuzzy({ intraMode: 1, intraIns: 1, interIns: Infinity });

let titles = [];      // string[] -- the haystack
let paths = [];       // string[] -- url path per post, parallel to titles
let blogId, points, day, topicMask, kindSource, score; // typed arrays
let hnId = null;      // deferred: hn.bin arrives after paths.txt
let blogs = [];       // per-blog metadata
let ready = false;

// Narrowing state: the last query and its PRE-FACET match set. Facets never
// affect the fuzzy set, so a prefix extension can always narrow from here.
let lastQuery = "";
let lastIdxs = null;
let termHits = null;   // set when the OR fallback fired: idx -> terms matched

const DAY0 = Date.UTC(2006, 0, 1) / 86400000;

async function get(base, name) {
  const res = await fetch(base + name);
  // A 404 on titles.txt does not reject -- res.text() happily returns the HTML
  // error page and the index goes live silently corrupt. Check status.
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  return res;
}

async function load(base) {
  // Retry re-enters here; without this the second run appends a whole second
  // copy of the corpus to the array it is still holding.
  titles = [];
  ready = false;
  const [metaRes, blogsRes] = await Promise.all([
    get(base, "meta.json"),
    get(base, "blogs.json"),
  ]);
  const meta = await metaRes.json();
  blogs = await blogsRes.json();

  // Strictly sequential, and the order is load-bearing. Nothing can be ranked,
  // filtered or rendered without the whole of posts.bin, whereas titles are
  // usable the moment the first chunk lands -- so posts.bin gets the pipe to
  // itself. Fetching both at once only splits the bandwidth and delays the one
  // that gates readiness: measured 4.6s concurrent vs 1.9s sequential at 9 Mbps.
  const binRes = await get(base, "posts.bin");
  const buf = await binRes.arrayBuffer();

  const n = meta.n_posts;
  if (buf.byteLength < n * 12) {
    throw new Error(`posts.bin truncated: ${buf.byteLength} < ${n * 12}`);
  }
  let o = 0;
  blogId = new Uint32Array(buf, o, n); o += n * 4;
  points = new Uint16Array(buf, o, n); o += n * 2;
  day = new Uint16Array(buf, o, n); o += n * 2;
  topicMask = new Uint16Array(buf, o, n); o += n * 2;
  kindSource = new Uint8Array(buf, o, n); o += n;
  score = new Uint8Array(buf, o, n);

  // Stream the titles instead of awaiting all 3.2MB of them.
  //
  // build_index.py orders every column by descending score, so the bytes that
  // arrive first are the highest-ranked slice of the corpus rather than an
  // arbitrary one. Search goes live on the first chunk and every pool loop is
  // already bounded by titles.length, so a partial array narrows the corpus
  // without ever producing a wrong row -- only a smaller set of right ones.
  // 9 Mbps time-to-searchable: 5.4s -> under 2s.
  await streamTitles(await get(base, "titles.txt"), meta, n);

  // paths.txt is 3.1MB gzipped and is only needed to build an href, never to
  // match, so it must not gate readiness. It must also not START before
  // readiness: fetched alongside titles.txt it competed for the same pipe and
  // pushed time-to-searchable from 4.5s to 8.2s on a 9 Mbps connection --
  // the "deferred" fetch was costing nearly as much as awaiting it. Until it
  // lands, emit() yields an empty path and rows link to the blog homepage.
  // Sequential again, and paths first: an absent HN link is invisible, whereas
  // an absent path silently points the row at the blog's homepage instead of
  // the article. Fix the wrong link before adding the missing one.
  get(base, "paths.txt")
    .then((r) => r.text())
    .then((t) => {
      paths = t.split("\n");
      postMessage({ type: "paths-ready" });
      return get(base, "hn.bin");
    })
    .then((r) => r.arrayBuffer())
    .then((b) => {
      if (b.byteLength >= n * 4) {
        hnId = new Uint32Array(b, 0, n);
        postMessage({ type: "hn-ready" });
      }
    })
    .catch(() => {});   // links degrade to the blog home; search still works
}

async function streamTitles(res, meta, n) {
  const announce = () => {
    if (ready) return;
    ready = true;
    postMessage({ type: "ready", meta, nTitles: titles.length, blogs });
  };
  const grew = () => {
    // Any cached match set is now missing the titles that just arrived, and
    // narrowing from it would silently hide them for the rest of the session.
    lastQuery = "";
    lastIdxs = null;
  };

  if (!res.body || !res.body.getReader) {
    titles = (await res.text()).split("\n");
    grew();
    announce();
    postMessage({ type: "titles-complete", loaded: titles.length, total: n });
    return;
  }

  const reader = res.body.getReader();
  const dec = new TextDecoder("utf-8");
  let carry = "";
  let lastPing = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    // stream:true so a multi-byte character split across chunk boundaries is
    // held back rather than decoded into a replacement character.
    const parts = (carry + dec.decode(value, { stream: true })).split("\n");
    carry = parts.pop();          // may be a partial line; hold it for next chunk
    if (parts.length) {
      for (let i = 0; i < parts.length; i++) titles.push(parts[i]);
      grew();
      announce();
      const now = Date.now();
      if (now - lastPing > 400) {
        lastPing = now;
        postMessage({ type: "progress", loaded: titles.length, total: n });
      }
    }
  }
  if (carry) titles.push(carry);  // last line carries no trailing newline
  grew();
  announce();
  postMessage({ type: "titles-complete", loaded: titles.length, total: n });
}

/* Top-N by key without sorting the whole pool.
 *
 * Browse mode ("newest", no query) has a pool of every post in the corpus.
 * pool.sort(cmp).slice(0, limit) on 111k rows costs tens of ms for 40 visible
 * results. This keeps a sorted buffer of `limit` items and binary-inserts only
 * the rare candidate that beats the running threshold, so the common case is
 * one comparison per row. */
function topN(pool, key, limit) {
  if (pool.length <= limit) return pool.slice().sort((a, b) => key(b) - key(a));
  const best = [];
  let thresh = -Infinity;
  for (let k = 0; k < pool.length; k++) {
    const i = pool[k];
    const v = key(i);
    if (best.length === limit && v <= thresh) continue;
    let lo = 0, hi = best.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (key(best[mid]) >= v) lo = mid + 1;
      else hi = mid;
    }
    best.splice(lo, 0, i);
    if (best.length > limit) best.pop();
    if (best.length === limit) thresh = key(best[limit - 1]);
  }
  return best;
}

/* Keep at most `cap` posts from any one blog in a page of results.
 * Without this, a single high-cadence publisher owns the entire Newest view --
 * cloudflarestatus.com filled all 8 top slots with per-datacenter notices. */
function diversify(ordered, cap, limit) {
  const seen = new Map();
  const kept = [];
  const overflow = [];
  for (const i of ordered) {
    const b = blogId[i];
    const c = seen.get(b) || 0;
    if (c < cap) {
      seen.set(b, c + 1);
      kept.push(i);
      if (kept.length === limit) return kept;
    } else if (overflow.length < limit) {
      overflow.push(i);
    }
  }
  // Backfill rather than return a short page when few blogs matched.
  for (const i of overflow) {
    if (kept.length === limit) break;
    kept.push(i);
  }
  return kept;
}

function passes(i, f) {
  if (f.blogId >= 0 && blogId[i] !== f.blogId) return false;
  if (f.topicMask && !(topicMask[i] & 0x0fff & f.topicMask)) return false;
  // sinceDay is a day offset from DAY0, precomputed on the main thread so this
  // stays one integer compare per post on the hot path.
  if (f.sinceDay && day[i] < f.sinceDay) return false;
  if (f.hideDead && (topicMask[i] >> 15) & 1) return false;
  const ks = kindSource[i];
  // An explicit source choice wins over the newsroom toggle. Otherwise asking
  // for Newsroom while newsrooms are hidden would return nothing, and the
  // reader would have to notice a second control to explain the first.
  // Both source cuts are skipped for a pinned blog: the pin IS the filter, and
  // the reader is looking at one named publisher. Without the blogId guard a
  // Source chip that excludes that publisher emptied the pinned view entirely
  // -- header, description and recommendations above zero posts.
  if (f.blogId < 0) {
    // An explicit source choice wins over the newsroom toggle. Otherwise asking
    // for Newsroom while newsrooms are hidden would return nothing, and the
    // reader would have to notice a second control to explain the first.
    if (f.sourceMask) {
      if (!((f.sourceMask >> (ks >> 3)) & 1)) return false;
    } else if (f.hideNews && (f.hiddenSourceMask >> (ks >> 3)) & 1) {
      return false;
    }
  }
  if (f.kindMask && !((f.kindMask >> (ks & 7)) & 1)) return false;
  return true;
}

function emit(ordered, total) {
  const rows = ordered.map((i) => ({
    i,
    t: titles[i],
    b: blogId[i],
    p: points[i],
    u: paths[i] || "",
    d: (day[i] + DAY0) * 86400000,
    k: kindSource[i] & 7,
    s: kindSource[i] >> 3,
    h: hnId ? hnId[i] : 0,   // hn.bin is deferred; no id yet means no link yet
    kr: (topicMask[i] >> 14) & 1,   // kind came from a title rule, not a fallback
    fd: (topicMask[i] >> 13) & 1,   // sourced from the blog's feed, not from HN
    dead: (topicMask[i] >> 15) & 1,
  }));
  return { rows, total };
}

function searchPosts(q, f, limit, sortMode) {
  let idxs;
  if (!q) {
    // Clear the union weights from the PREVIOUS query. They are module state
    // and browse mode used to never read them, so a stale map was invisible --
    // until the explicit sorts started tiering on it and an emptied search box
    // produced points order that was not descending.
    termHits = null;
    idxs = null; // browse mode -- rank everything by baked score
  } else {
    const extend = lastQuery && q.startsWith(lastQuery) && lastIdxs;
    idxs = uf.filter(titles, q, extend ? lastIdxs : undefined);

    // uFuzzy requires EVERY term to be present, so "sqlite internals" matched
    // exactly one title even though hundreds are relevant. When the strict pass
    // is that thin, union the per-term matches instead and rank by how many
    // terms each title hit -- strict results still sort first because they hit
    // all of them.
    const terms = q.split(/\s+/).filter((t) => t.length > 2);
    // Widen whenever the strict pass cannot fill a page, not just when it is
    // nearly empty. uFuzzy matches terms in order, so "writing a compiler"
    // found 34 titles and missed "A Compiler Writing Playground" -- and at 34,
    // one over the old threshold of 25, it never widened. Six slots sat empty
    // on a 40-row page with relevant results available. Strict matches are
    // scored terms.length + 1, so widening only ever appends below them.
    if (terms.length > 1 && (!idxs || idxs.length < 40)) {
      const hits = new Map();
      for (const i of idxs || []) hits.set(i, terms.length + 1);
      for (const t of terms) {
        const ti = uf.filter(titles, t);
        if (!ti) continue;
        for (const i of ti) hits.set(i, (hits.get(i) || 0) + 1);
      }
      // With 3+ terms, a single-term match is mostly noise ("how"/"works"
      // match thousands of titles alone), so require at least two. With 2
      // terms, requiring two would just be the strict pass we are relaxing.
      const minHits = terms.length >= 3 ? 2 : 1;
      idxs = [...hits.keys()]
        .filter((i) => hits.get(i) >= minHits)
        .sort((a, b) => (hits.get(b) - hits.get(a)) || (a - b));
      termHits = hits;
      lastQuery = "";       // this set is not a strict prefix match; do not narrow from it
      lastIdxs = null;
    } else {
      termHits = null;
      lastQuery = q;
      lastIdxs = idxs;
    }
    if (!idxs || !idxs.length) return { rows: [], total: 0 };
  }

  // Facet pass. In browse mode this walks the corpus once; with a query it only
  // walks the match set, which is why filters stay cheap as you type.
  const pool = [];
  if (idxs) {
    for (let k = 0; k < idxs.length; k++) {
      const i = idxs[k];
      if (passes(i, f)) pool.push(i);
    }
  } else {
    for (let i = 0; i < titles.length; i++) {
      if (passes(i, f)) pool.push(i);
    }
  }
  const total = pool.length;
  if (!total) return { rows: [], total: 0 };

  // Explicit sorts decide ORDER, never membership -- but when the per-term
  // union has widened the pool, its members are not equal matches, and sorting
  // them as if they were buries the ones that matched the whole query.
  //
  // "writing a compiler" widens to 1,815 posts. Sorted purely by points, the
  // top rows became "Firefox's new streaming and tiering compiler" and
  // "Writing one sentence per line" -- each matching one word -- while
  // "Writing a C compiler in 500 lines of Python" fell off the page. Sorting
  // by date was worse: "Bookmarks - llm, music, writing, synth".
  //
  // So sort within match tier: every post matching more of the query outranks
  // every post matching less, and the chosen sort orders each tier. Asking for
  // "most upvoted" still means most upvoted -- among the posts that actually
  // match. TIER is larger than any points or day value (both u16).
  const TIER = 1 << 17;
  const tier = termHits ? (i) => (termHits.get(i) || 0) * TIER : () => 0;
  const cap = f.blogId >= 0 ? Infinity : 3;
  const wide = cap === Infinity ? limit : limit * 5;
  if (sortMode === "points") {
    return emit(diversify(topN(pool, (i) => tier(i) + points[i], wide), cap, limit), total);
  }
  if (sortMode === "date") {
    return emit(diversify(topN(pool, (i) => tier(i) + day[i], wide), cap, limit), total);
  }
  if (sortMode === "oldest") {
    // Ascending day inside a descending tier: flip the day, do not negate the
    // whole key, or a better tier would lose to an older post.
    return emit(diversify(topN(pool, (i) => tier(i) + (65535 - day[i]), wide), cap, limit), total);
  }

  let ordered;
  if (!q) {
    ordered = diversify(topN(pool, (i) => score[i], wide), cap, limit);
  } else {
    // info()/sort() cost scales with the match set, and a one-letter query
    // matches most of the corpus. Bound it: keep the highest-scoring INFO_CAP
    // matches, then rank those by match quality. We only ever render ~50 rows,
    // so the headroom is generous.
    const INFO_CAP = 3000;
    let cand = pool;
    if (cand.length > INFO_CAP) {
      // Keep the best MATCHES, not merely the highest-scoring rows. On the
      // widened path the pool holds one-term matches whose baked score can
      // exceed an every-term match's, so cutting by score alone could discard
      // the very rows the query is about before ranking ever sees them.
      cand = termHits
        ? pool.slice()
            .sort((a, b) => (termHits.get(b) || 0) - (termHits.get(a) || 0) ||
                            score[b] - score[a])
            .slice(0, INFO_CAP)
        : pool.slice().sort((a, b) => score[b] - score[a]).slice(0, INFO_CAP);
    }
    cand.sort((a, b) => a - b); // info() expects ascending haystack indices

    // uFuzzy's own ordering is match quality. Rather than replicate its
    // comparator, bucket by quality tier and rank by baked post score inside a
    // tier: best matches first, and popular posts first among equally good
    // matches. An exact-ish match can never be buried by a merely popular one.
    if (termHits) {
      // Fallback path: rank by terms matched, then by baked score. info()/sort()
      // would re-impose the all-terms ordering we just relaxed.
      ordered = cand
        .sort((a, b) => (termHits.get(b) - termHits.get(a)) || (score[b] - score[a]))
        .slice(0, limit);
      return emit(ordered, total);
    }
    const info = uf.info(cand, titles, q);
    const order = uf.sort(info, titles, q);
    const rank = new Map();
    // info.idx[k] is ALREADY a haystack index (uFuzzy sets info.idx[b] = idxs[n]),
    // and our haystack is the full `titles` array. Indexing `cand` with it again
    // was a second bogus lookup that yielded undefined for nearly every entry,
    // leaving the rank map empty and collapsing "Best" into pure score order.
    for (let r = 0; r < order.length; r++) rank.set(info.idx[order[r]], r);
    const tier = (i) => {
      const r = rank.get(i);
      return r === undefined ? 3 : r < 200 ? 0 : r < 1000 ? 1 : 2;
    };
    ordered = cand
      .sort((a, b) => {
        const ta = tier(a), tb = tier(b);
        return ta !== tb ? ta - tb : score[b] - score[a];
      })
      .slice(0, limit);
  }

  return emit(ordered, total);
}

/* ---------- "more like this" ----------
 *
 * Finding one blog you like is the easy half; the useful question right after
 * is what else reads like it. Built from the one-line descriptions, which are
 * the only per-blog prose in the index, as TF-IDF cosine similarity nudged by
 * topic overlap and source class.
 *
 * Two rules do most of the work:
 *   - The blog's own domain is NOT part of the document. It matches on spelling
 *     rather than subject.
 *   - A candidate must share at least TWO weighted terms. One shared rare term
 *     is nearly always a person's name -- rachelbythebay.com matched
 *     rachel.fast.ai, jvns.ca matched Eric Evans and Benedict Evans, all on a
 *     surname and nothing else. Two shared terms is a topic.
 */
const SIM_STOP = new Set(
  ("the a an and or of to in for on with by from is are this that its his her their " +
   "blog posts about writing notes site website essays personal").split(" "),
);
let simVec = null;      // Map(term -> weight)[] per blog
let simInv = null;      // Map(term -> blog index[])

function buildSimilarity() {
  if (simVec) return;
  const docs = blogs.map((b) =>
    ((b.o || "").toLowerCase().match(/[a-z0-9+#]{3,}/g) || [])
      .filter((w) => !SIM_STOP.has(w)),
  );
  const df = new Map();
  for (const d of docs) for (const w of new Set(d)) df.set(w, (df.get(w) || 0) + 1);
  const N = docs.length;
  simVec = [];
  simInv = new Map();
  for (let i = 0; i < N; i++) {
    const tf = new Map();
    for (const w of docs[i]) tf.set(w, (tf.get(w) || 0) + 1);
    const v = new Map();
    let sq = 0;
    for (const [w, c] of tf) {
      const idf = Math.log(N / df.get(w));
      if (idf <= 1.5) continue;         // a term two thirds of blogs share says nothing
      const x = (1 + Math.log(c)) * idf;
      v.set(w, x);
      sq += x * x;
    }
    const nrm = Math.sqrt(sq) || 1;
    for (const [w, x] of v) {
      v.set(w, x / nrm);
      if (!simInv.has(w)) simInv.set(w, []);
      simInv.get(w).push(i);
    }
    simVec.push(v);
  }
}

const popcount = (x) => {
  let c = 0;
  while (x) { x &= x - 1; c++; }
  return c;
};

/* Same organisation: blog.cloudflare.com, cloudflare.com and
 * radar.cloudflare.com are one publisher, and filling the list with a company's
 * own properties is not a recommendation. */
const org = (name) => name.split("/")[0].split(".").slice(-2).join(".");

function similarBlogs(i, k, f) {
  buildSimilarity();
  if (i < 0 || i >= blogs.length) return [];
  // Respect the newsroom toggle, so a reader who hid newsrooms is not sent to
  // one. The exception is a reader already looking AT a hidden source: they
  // reached it deliberately, and answering "what else is like The Spectator"
  // with nothing at all would be worse than answering it with other magazines.
  // Recommend only blogs the reader could actually reach. A chip pointing at a
  // blog the current filters exclude leads to a pinned page with zero posts.
  const selfExcluded = f && (
    (f.sourceMask && !((f.sourceMask >> blogs[i].s) & 1)) ||
    (!f.sourceMask && f.hideNews && ((f.hiddenSourceMask >> blogs[i].s) & 1)));
  const drop = (b) => {
    if (!f || selfExcluded) return false;   // already looking at an excluded blog
    if (f.sourceMask) return !((f.sourceMask >> b.s) & 1);
    return f.hideNews && ((f.hiddenSourceMask >> b.s) & 1);
  };
  const score = new Map(), shared = new Map();
  for (const [w, x] of simVec[i]) {
    const list = simInv.get(w);
    if (!list || list.length > 400) continue;   // too common to discriminate
    for (const j of list) {
      if (j === i) continue;
      const y = simVec[j].get(w);
      if (!y) continue;
      score.set(j, (score.get(j) || 0) + x * y);
      shared.set(j, (shared.get(j) || 0) + 1);
    }
  }
  const bi = blogs[i], myOrg = org(bi.n);
  const out = [];
  for (const [j, raw] of score) {
    if ((shared.get(j) || 0) < 2) continue;
    const bj = blogs[j];
    if (drop(bj)) continue;
    const inter = popcount(bi.tm & bj.tm), union = popcount(bi.tm | bj.tm) || 1;
    let sc = raw * (0.55 + (0.45 * inter) / union);
    if (bi.s === bj.s) sc *= 1.12;
    out.push({ j, sc, same: org(bj.n) === myOrg });
  }
  out.sort((a, b) => b.sc - a.sc);
  const kept = [];
  let sameOrg = 0;
  for (const r of out) {
    if (r.same && ++sameOrg > 1) continue;   // one sibling property at most
    kept.push({ i: r.j, ...blogs[r.j] });
    if (kept.length === k) break;
  }
  return kept;
}

function searchBlogs(q, f, limit, sortMode) {
  let pool = [];
  for (let i = 0; i < blogs.length; i++) {
    const b = blogs[i];
    if (f.topicMask && !(b.tm & f.topicMask)) continue;
    if (f.sourceMask) {
      if (!((f.sourceMask >> b.s) & 1)) continue;
    } else if (f.hideNews && (f.hiddenSourceMask >> b.s) & 1) continue;
    // For a blog, "since" means still active: `l` is the year it was last seen
    // on HN. This is the only way to ask for blogs that have not gone quiet.
    if (f.sinceYear && b.l < f.sinceYear) continue;
    if (f.needFeed && !b.f) continue;
    pool.push(i);
  }
  if (q) {
    const hay = pool.map((i) => blogs[i].n + " " + blogs[i].o);
    const idxs = uf.filter(hay, q);
    if (!idxs || !idxs.length) return { rows: [], total: 0 };
    const info = uf.info(idxs, hay, q);
    const order = uf.sort(info, hay, q);
    pool = order.map((r) => pool[info.idx[r]]);
  }
  // Capture the true match count BEFORE any truncation: topN returns at most
  // `limit` items, so reading pool.length afterwards reported "40 blogs" for
  // every query and made render() hide "Show more" (40 >= 40), stranding all
  // but the first page. searchPosts already captures total up front.
  const total = pool.length;

  // For blogs, "upvotes" means the blog's median HN score and "newest" means
  // most recently seen on HN -- the nearest honest equivalents.
  if (sortMode === "points") pool = topN(pool, (i) => blogs[i].m, limit);
  else if (sortMode === "date") pool = topN(pool, (i) => blogs[i].l, limit);
  else if (sortMode === "oldest") pool = topN(pool, (i) => -blogs[i].l, limit);
  else if (sortMode === "quality") pool = topN(pool, (i) => blogs[i].q, limit);
  else if (!q) pool = topN(pool, (i) => blogs[i].q, limit);
  return {
    rows: pool.slice(0, limit).map((i) => ({ i, ...blogs[i] })),
    total,
  };
}

onmessage = (e) => {
  const m = e.data;
  if (m.type === "load") {
    // A rejected promise inside a worker does NOT trigger worker.onerror, so
    // without this the page sits on "Loading index..." forever, in silence.
    load(m.base).catch((err) =>
      postMessage({ type: "error", message: String((err && err.message) || err) }),
    );
    return;
  }
  if (!ready) return;
  if (m.type === "similar") {
    postMessage({ type: "similar", blog: m.blog,
                  rows: similarBlogs(m.blog, m.k || 5, m.filters) });
    return;
  }
  if (m.type === "export") {
    // The whole matching blogroll, not the rendered page -- an OPML of 40 rows
    // would silently be a fraction of what the filters describe. Ranked by
    // quality and capped, because no one imports 3,000 feeds into a reader.
    const f = { ...m.filters, needFeed: true };
    const r = searchBlogs(m.q, f, m.cap, "quality");
    postMessage({
      type: "export",
      total: r.total,
      rows: r.rows.map((b) => ({ n: b.n, h: b.h, o: b.o, f: b.f })),
    });
    return;
  }
  if (m.type === "query") {
    const t0 = performance.now();
    const r =
      m.mode === "blogs"
        ? searchBlogs(m.q, m.filters, m.limit, m.sort)
        : searchPosts(m.q, m.filters, m.limit, m.sort);
    postMessage({
      type: "results",
      seq: m.seq,
      mode: m.mode,
      ms: performance.now() - t0,
      ...r,
    });
  }
};
