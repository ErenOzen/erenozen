#!/usr/bin/env python3
"""Discover and fetch RSS/Atom feeds for a list of blogs.

Why this exists alongside the HN data: HN only ever surfaces the posts that
happened to go viral. A blog's best writing frequently never hits the front
page. Feeds give us the blog's own view of what it published.

Caveat baked into expectations: most feeds are truncated to the latest 10-50
entries, so this yields freshness and depth-of-recent, not full archives.
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import feedparser
import requests

UA = "Mozilla/5.0 (compatible; blogfinder/0.1; +https://erenozen.dev)"
TIMEOUT = 15
CANDIDATE_PATHS = [
    "/feed", "/feed/", "/rss", "/rss/", "/feed.xml", "/rss.xml", "/atom.xml",
    "/index.xml", "/feeds/all.atom.xml", "/blog/feed", "/blog/rss",
    "/blog/index.xml", "/posts/index.xml", "/feed/atom",
]
LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I
)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)

session = requests.Session()
session.headers.update({"User-Agent": UA})


def _get(url, **kw):
    return session.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)


def discover_feed(home):
    """Return a feed URL for a blog home page, or None."""
    host = urlparse(home).netloc.lower()

    # Platform shortcuts -- cheaper and more reliable than sniffing.
    if host.endswith(".substack.com"):
        return urljoin(home, "/feed")
    # A Medium subdomain serves its own feed. Returning None for its empty path
    # made every <name>.medium.com blog a permanent "no feed" in feed_urls.tsv
    # without a single request.
    if host.endswith(".medium.com"):
        return f"https://{host}/feed"
    if host == "medium.com":
        p = urlparse(home).path.strip("/")
        return f"https://medium.com/feed/{p}" if p else None
    if host == "dev.to":
        p = urlparse(home).path.strip("/")
        return f"https://dev.to/feed/{p}" if p else None

    # 1) Ask the homepage what its feed is.
    try:
        r = _get(home)
        if r.ok and r.text:
            for tag in LINK_RE.findall(r.text[:200_000]):
                m = HREF_RE.search(tag)
                if m:
                    return urljoin(r.url, m.group(1))
    except requests.RequestException:
        pass

    # 2) Fall back to conventional locations.
    for path in CANDIDATE_PATHS:
        try:
            r = _get(urljoin(home, path))
            ctype = r.headers.get("content-type", "").lower()
            if r.ok and ("xml" in ctype or r.text.lstrip()[:200].startswith("<?xml")):
                return r.url
        except requests.RequestException:
            continue
    return None


def parse_feed(feed_url):
    """Return (entries, feed_title, final_url). Entries are dicts.

    final_url is where the feed lives after redirects: a blog that moved keeps
    its old feed URL in the seed, and only the final one says where it went.
    """
    try:
        r = _get(feed_url)
        if not r.ok:
            return [], None, None
        d = feedparser.parse(r.content)
    except (requests.RequestException, Exception):
        return [], None, None

    out = []
    for e in d.entries[:200]:
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                try:
                    ts = int(time.mktime(e[key]))
                except (TypeError, ValueError, OverflowError):
                    pass
                break
        summary = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "")
        summary = re.sub(r"\s+", " ", summary).strip()[:300]
        # A link blog's entry links to the article it is about; the blog's own
        # page for the item rides along as rel="related" (daringfireball.net)
        # or as the id (waxy.org, sebsauvage.net). Keep the candidates and let
        # build_index choose -- only it knows the blog's home, and a feed can
        # live on another host entirely (feedburner).
        alt = []
        for l in e.get("links") or []:
            h = l.get("href")
            if h and h != link and l.get("rel") in ("alternate", "related") and h not in alt:
                alt.append(h)
        for k in ("id", "feedburner_origlink"):
            v = e.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")) \
                    and v != link and v not in alt:
                alt.append(v)
        # Always present, even empty: build_index measures a link blog over the
        # entries fetched since this field existed, and needs to tell them apart.
        out.append({"title": title, "url": link, "published": ts, "summary": summary,
                    "alt": alt[:4]})
    return out, (d.feed.get("title") if d.get("feed") else None), r.url


# Bumped when records gain a field the build needs. A record written by an older
# version counts as never fetched, so the next run refetches it instead of
# waiting REFRESH_DAYS for the field to appear. 2: entries carry "alt" and
# records carry "feed_final".
FEED_SCHEMA = 2

DEADLINE = None       # set by main() when TIME_BUDGET is given
KNOWN = {}            # key -> feed URL (or None for "no feed"), from feed_urls.tsv
SKIP_NEGATIVE = os.environ.get("SKIP_NEGATIVE", "1") not in ("0", "", "false")


def load_known(path):
    """Feed URLs resolved by an earlier run.

    Discovery is the expensive half of this script: for a blog whose HTML does
    not advertise a feed, it tries up to 14 candidate paths, each a request with
    a 15s timeout. Re-deriving an answer we already have, every month, is what
    made a full pass cost four hours. A seeded blog costs one request.
    """
    if not os.path.exists(path):
        return {}
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        k, _, u = line.partition("\t")
        if k and u:
            out[k] = None if u == "-" else u    # "-" means: discovery found nothing
    return out


def handle(blog):
    # Checked inside the worker, not around the submit loop: every blog is
    # submitted up front, so the only way to stop early is to let the queued
    # ones fall through. A skipped blog writes nothing, so the next run picks
    # it up exactly as if it had never been queued.
    if DEADLINE and time.time() > DEADLINE:
        return None
    home = blog["home"]
    try:
        if blog["key"] in KNOWN and KNOWN[blog["key"]] is None and SKIP_NEGATIVE:
            # Known to have no feed. Skipping costs one blog its (already nil)
            # chance of having added one since; NOT skipping costs 14 timing-out
            # requests, times 4,350 blogs, every month. Run with SKIP_NEGATIVE=0
            # to re-derive them and regenerate the seed.
            return {**blog, "feed": None, "entries": [], "error": "no-feed"}
        seeded = KNOWN.get(blog["key"])
        if seeded:
            entries, ftitle, final = parse_feed(seeded)
            if entries:
                return {**blog, "feed": seeded, "feed_final": final, "feed_title": ftitle,
                        "entries": entries, "error": None}
            # The seeded URL has stopped working -- a blog moved platforms, or
            # dropped its feed. Fall through to a full discovery rather than
            # trusting a stale answer forever.
        feed_url = discover_feed(home)
        if not feed_url:
            return {**blog, "feed": None, "entries": [], "error": "no-feed"}
        entries, ftitle, final = parse_feed(feed_url)
        return {**blog, "feed": feed_url, "feed_final": final, "feed_title": ftitle,
                "entries": entries, "error": None if entries else "empty"}
    except Exception as e:  # never let one blog kill the crawl
        return {**blog, "feed": None, "entries": [], "error": f"{type(e).__name__}"}


def main():
    global DEADLINE
    src, out_path = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 24

    # A full pass over every classified blog runs at ~52 blogs/min, so 13.4k
    # blogs is roughly four hours -- far past any CI job's patience. With a
    # budget the run always ends on time and the next one continues, because
    # output is append-only and keyed by blog.
    budget = int(os.environ.get("TIME_BUDGET", "0"))
    if budget:
        DEADLINE = time.time() + budget
    # 0 means "never refetch", which is right for a one-off backfill and wrong
    # for a monthly refresh: the whole point of feeds is freshness, and a record
    # fetched once would be quoted forever.
    refresh_days = float(os.environ.get("REFRESH_DAYS", "0"))
    now = time.time()

    global KNOWN
    KNOWN = load_known(os.environ.get(
        "FEED_URLS",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_urls.tsv")))
    if KNOWN:
        print(f"seeded with {len(KNOWN):,} known feed URLs", flush=True)

    blogs = [json.loads(l) for l in open(src)]

    # Compact before anything else. With REFRESH_DAYS the file gains a record
    # per blog per run, so a year of monthly refreshes is twelve copies of 13k
    # blogs -- roughly a gigabyte, carried through the CI cache and loaded whole
    # by build_index.py. Two generations is enough: build_index unions their
    # entries, so the older one only contributes posts that have since scrolled
    # out of the feed window. Steady state is one record per unrefreshed blog
    # and two for each blog this run touched.
    if os.path.exists(out_path):
        by_key = {}
        total = 0
        for line in open(out_path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            total += 1
            by_key.setdefault(r.get("key"), []).append(r)
        # A record fetched from a feed URL the seed no longer names is stale,
        # however young. medium.com/@steve.yegge's seed was repointed off an old
        # @handle that now belongs to a spam account: the cached record from that
        # feed would have been reused for a month, spam post included, and
        # update_feed_urls would have written the spam feed back into the seed.
        # Dropped, the key counts as never fetched and is fetched first.
        def stale(r):
            s = KNOWN.get(r.get("key"))
            return bool(s and r.get("feed") and r["feed"] != s)

        n_stale = sum(stale(r) for rows in by_key.values() for r in rows)
        # Trim to one record per key BEFORE the run, so that afterwards there
        # are at most two: the previous generation and this one. Trimming to two
        # here instead lets the run add a third, which is how "two generations"
        # quietly becomes unbounded.
        if total > len(by_key) or n_stale:
            with open(out_path, "w") as f:
                for rows in by_key.values():
                    rows = [r for r in rows if not stale(r)]
                    if not rows:
                        continue
                    # Keep the newest record that HAS entries; fall back to the
                    # newest only when no generation has any. handle() writes an
                    # empty record for every failure -- a 500, a rate limit, a
                    # timeout -- and stamps it with a fresh fetched_at, so
                    # "newest" alone kept the failure and deleted the last good
                    # generation, before the run could replace it. build_index
                    # skips empty records, so the blog's feed posts and its
                    # subscribe link vanished on one transient error.
                    best = max(rows, key=lambda r: (bool(r.get("entries")),
                                                    r.get("fetched_at") or 0))
                    f.write(json.dumps(best, ensure_ascii=False) + "\n")
            print(f"compacted: {total} records -> one per key "
                  f"({n_stale} from a feed the seed no longer names, dropped)", flush=True)

    fetched_at = {}
    if os.path.exists(out_path):
        for line in open(out_path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = r.get("key")
            if k and r.get("v", 1) >= FEED_SCHEMA:
                fetched_at[k] = max(fetched_at.get(k, 0), r.get("fetched_at") or 0)
        if fetched_at:
            print(f"resuming: {len(fetched_at)} blogs already fetched", flush=True)

    def age(b):
        return now - fetched_at.get(b["key"], 0)

    if refresh_days:
        cutoff = refresh_days * 86400
        blogs = [b for b in blogs if age(b) >= cutoff]
    else:
        blogs = [b for b in blogs if b["key"] not in fetched_at]

    # Never-fetched blogs first (a newly classified blog is invisible until it
    # lands), then stalest first, so the long tail cycles instead of starving
    # behind the same head every month.
    blogs.sort(key=lambda b: -age(b))

    print(f"fetching feeds for {len(blogs)} blogs with {workers} workers"
          + (f", {budget}s budget" if budget else ""), flush=True)

    done = ok = skipped = 0
    with open(out_path, "a") as out, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(handle, b): b for b in blogs}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:          # past the deadline; leave it for next time
                skipped += 1
                continue
            done += 1
            if r["entries"]:
                ok += 1
            r["fetched_at"] = int(time.time())
            r["v"] = FEED_SCHEMA
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            if done % 100 == 0:
                out.flush()
                print(f"  {done}/{len(blogs)} done, {ok} with entries", flush=True)

    print(f"DONE: {ok}/{done} blogs yielded feed entries"
          + (f" ({skipped} left for the next run)" if skipped else ""))


if __name__ == "__main__":
    main()
