#!/usr/bin/env python3
"""Where a feed post links: crafted feeds through the real parser and build.

Each case is a blog the link-blog rule in build_index.py broke, or would have
broken, before a guard was added -- found by adversarial review, reproduced
there, and kept here so a later edit cannot quietly reopen it. No network:
fetch_feeds.parse_feed gets a stubbed HTTP response.
"""
import contextlib, io, json, os, shutil, struct, sys, tempfile, time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_index, fetch_feeds

NOW = int(time.time())
EXT = ["github.com/a/b", "www.youtube.com/watch?v=z", "9to5mac.com/x", "www.nytimes.com/y",
       "lwn.net/Articles/1", "arstechnica.com/z", "www.theverge.com/q", "stratechery.com/w"]
failures = []


class _Resp:
    def __init__(self, body, url):
        self.ok, self.content, self.text, self.url = True, body.encode(), body, url


def rss(items):
    out = []
    for i, it in enumerate(items):
        ts = NOW - (i + 1) * 30 * 3600          # 30h apart, so never a firehose
        guid = f'<guid isPermaLink="false">{it["guid"]}</guid>' if it.get("guid") else ""
        rel = f'<atom:link rel="related" href="{it["related"]}"/>' if it.get("related") else ""
        out.append(f"<item><title>{it['title']}</title><link>{it['link']}</link>"
                   f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(ts))}"
                   f"</pubDate>{guid}{rel}</item>")
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
            f"<channel><title>T</title><link>https://x.example/</link>{''.join(out)}"
            "</channel></rss>")


LAST_HOMES = {}   # title -> home of the blog the last build() filed it under


def build(key, home, feed, final, items, others=(), others_first=False):
    """Index one personal blog from a crafted feed; return {title: post URL}.

    others: (key, home) or (key, home, items) of further blogs to index, so the
    build knows them as blogs; with items they get a feed of their own."""
    d = tempfile.mkdtemp(prefix="feedlinks-")
    try:
        cls = os.path.join(d, "cls")
        os.makedirs(cls)
        fetch_feeds._get = lambda url, **kw: _Resp(rss(items), final)
        entries, _, fin = fetch_feeds.parse_feed(feed)
        with open(os.path.join(d, "cands.jsonl"), "w") as fc, \
                open(os.path.join(cls, "t.jsonl"), "w") as fk:
            for o in [(key, home), *others]:
                fc.write(json.dumps({"key": o[0], "home": o[1], "n_stories": 5,
                                     "median_points": 50, "last_seen": NOW}) + "\n")
                fk.write(json.dumps({"key": o[0], "source": "personal", "is_programming_blog": True,
                                     "topics": [{"slug": "web", "weight": 1.0}]}) + "\n")
        lines = [json.dumps({"key": key, "home": home, "feed": feed, "feed_final": fin,
                             "entries": entries, "fetched_at": NOW, "v": 2})]
        for o in others:
            if len(o) > 2 and o[2]:
                ofeed = o[1] + "/feed"
                fetch_feeds._get = lambda url, _x=o[2], _f=ofeed, **kw: _Resp(rss(_x), _f)
                oents, _, ofin = fetch_feeds.parse_feed(ofeed)
                lines.append(json.dumps({"key": o[0], "home": o[1], "feed": ofeed, "feed_final": ofin,
                                         "entries": oents, "fetched_at": NOW, "v": 2}))
        if others_first:
            lines = lines[1:] + lines[:1]
        with open(os.path.join(d, "feeds.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        open(os.path.join(d, "dedup.jsonl"), "w").close()
        out = os.path.join(d, "out")
        sys.argv = ["build_index.py", os.path.join(d, "dedup.jsonl"),
                    os.path.join(d, "cands.jsonl"), cls, out, os.path.join(d, "feeds.jsonl")]
        with contextlib.redirect_stdout(io.StringIO()):
            build_index.main()
        blogs = json.load(open(os.path.join(out, "blogs.json")))
        paths = open(os.path.join(out, "paths.txt"), encoding="utf-8").read().split("\n")
        titles = open(os.path.join(out, "titles.txt"), encoding="utf-8").read().split("\n")
        n = json.load(open(os.path.join(out, "meta.json")))["n_posts"]
        bid = struct.unpack_from(f"<{n}I", open(os.path.join(out, "posts.bin"), "rb").read(), 0)
        LAST_HOMES.clear()
        LAST_HOMES.update({titles[i]: blogs[bid[i]]["h"] for i in range(n)})
        return {titles[i]: build_index.post_url(blogs[bid[i]]["h"], paths[i]) for i in range(n)}
    finally:
        shutil.rmtree(d)


def check(ok, what, detail=""):
    print(("  ok   " if ok else "  FAIL ") + what + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(what)


def host(u):
    return urlparse(u).netloc


def main():
    # A Medium subdomain serves its own feed. discover_feed returned None for
    # its empty path, and the refresh recorded a permanent "no feed".
    calls = []

    def no_request(*a, **k):
        calls.append(a)
        raise AssertionError("discover_feed made a request")

    real_get = fetch_feeds.session.get
    fetch_feeds.session.get = no_request
    try:
        sub = fetch_feeds.discover_feed("https://onezero.medium.com")
        handle = fetch_feeds.discover_feed("https://medium.com/@bellmar")
    finally:
        fetch_feeds.session.get = real_get
    check(sub == "https://onezero.medium.com/feed" and not calls,
          "a Medium subdomain's feed is found without a request", str(sub))
    check(handle == "https://medium.com/feed/@bellmar", "...and a Medium @handle's still is", str(handle))

    # A link blog (daringfireball.net's shape): each entry links to the article
    # it is about and names the blog's own page for it as rel="related".
    items = [{"title": f"Link {i}", "link": f"https://{EXT[i]}",
              "related": f"https://dftest.net/linked/2026/item-{i}"} for i in range(8)]
    items += [{"title": "Own essay", "link": "https://dftest.net/2026/essay"},
              {"title": "[Sponsor] Glyphs 4", "link": "https://glyphsapp.com/",
               "related": "https://dftest.net/feeds/sponsors/2026/glyphs"}]
    r = build("dftest.net", "https://dftest.net", "https://dftest.net/feeds/main",
              "https://dftest.net/feeds/main", items)
    check(all(r.get(f"Link {i}") == f"https://dftest.net/linked/2026/item-{i}" for i in range(8)),
          "a link blog's posts go to the blog's own page, not the article", r.get("Link 0", "missing"))
    check("[Sponsor] Glyphs 4" not in r, "a sponsor entry is not indexed as a post")

    # A WordPress blog indexed on its old domain that moved to a new one: its
    # guids still name the old domain. Two link-outs made it look like a link
    # blog, and every real post was swapped onto a dead old-domain ?p= URL.
    items = [{"title": f"Post {i}", "link": f"https://newname.org/2026/p{i}/",
              "guid": f"http://oldname.net/?p={100 + i}"} for i in range(10)]
    items += [{"title": f"Link {i}", "link": f"https://{EXT[i]}",
               "guid": f"http://oldname.net/?p={90 + i}"} for i in range(2)]
    r = build("oldname.net", "https://oldname.net", "https://newname.org/feed/",
              "https://newname.org/feed/", items)
    check(all(host(r[f"Post {i}"]) == "newname.org" for i in range(10)),
          "a moved blog's posts keep their new-domain links")
    # Same blog, but the seed still names the old feed URL: only the redirect
    # (feed_final) says where the blog went.
    r = build("oldname.net", "https://oldname.net", "https://oldname.net/feed/",
              "https://newname.org/feed/", items)
    check(all(host(r[f"Post {i}"]) == "newname.org" for i in range(10)),
          "...also when only the feed's redirect reveals the move")
    # A moved blog that really is a link blog: its own posts on the new domain
    # carry half the off-site entries, so that host is the blog's own.
    items = [{"title": f"Post {i}", "link": f"https://newname.org/2026/p{i}/",
              "guid": f"http://oldname.net/?p={100 + i}"} for i in range(6)]
    items += [{"title": f"Link {i}", "link": f"https://{EXT[i]}",
               "guid": f"http://oldname.net/?p={90 + i}"} for i in range(6)]
    r = build("oldname.net", "https://oldname.net", "https://oldname.net/feed/",
              "https://oldname.net/feed/", items)
    check(all(host(r[f"Post {i}"]) == "newname.org" for i in range(6)),
          "a moved link blog's own posts are never swapped")

    # ericwbailey.website gives every cross-post the id of its home page. All
    # of them became one row pointing at the home page; the rest were dropped.
    items = [{"title": f"Own {i}", "link": f"https://ewbtest.website/writing/{i}/"} for i in range(4)]
    items += [{"title": f"Cross {i}", "link": f"https://{EXT[i]}",
               "guid": "https://ewbtest.website/"} for i in range(8)]
    r = build("ewbtest.website", "https://ewbtest.website", "https://ewbtest.website/feed.xml",
              "https://ewbtest.website/feed.xml", items)
    check(len(r) == 12 and all(host(r[f"Cross {i}"]) != "ewbtest.website" for i in range(8)),
          "the home page is never an entry's own page", f"{len(r)} of 12 rows")

    # Ids that are #spots on one shared page, and a rel=related shared by every
    # entry: neither names one item, and both merged six posts into one.
    items = [{"title": f"Link {i}", "link": f"https://{EXT[i]}",
              "guid": f"https://anchortest.net/links/#item-{100 + i}"} for i in range(6)]
    r = build("anchortest.net", "https://anchortest.net", "https://anchortest.net/rss",
              "https://anchortest.net/rss", items)
    check(len(r) == 6 and all("anchortest.net" not in u for u in r.values()),
          "a #anchor id on a shared page is not an own page", f"{len(r)} of 6 rows")
    items = [{"title": f"Link {i}", "link": f"https://{EXT[i]}",
              "related": "https://sharetest.net/links/"} for i in range(6)]
    r = build("sharetest.net", "https://sharetest.net", "https://sharetest.net/rss",
              "https://sharetest.net/rss", items)
    check(len(r) == 6 and all("sharetest.net" not in u for u in r.values()),
          "a page several entries share is not an own page", f"{len(r)} of 6 rows")

    # jakewharton.com's cross-posts carry on-host ids that 404; they are a
    # quarter of its feed, which is not a link blog's shape.
    items = [{"title": f"Own {i}", "link": f"https://jaketest.com/own-{i}/"} for i in range(9)]
    items += [{"title": f"Cross {i}", "link": f"https://{EXT[i]}",
               "guid": f"https://jaketest.com/cross-{i}/"} for i in range(3)]
    r = build("jaketest.com", "https://jaketest.com", "https://jaketest.com/atom.xml",
              "https://jaketest.com/atom.xml", items)
    check(all("jaketest.com" not in r[f"Cross {i}"] for i in range(3)),
          "a feed that is mostly its own posts keeps its cross-posts' links")

    # A planet feed (ocaml.org's shape) carries other blogs' posts. One the
    # author's own feed indexes is the author's; one no other blog indexes stays
    # with the planet -- dropping those outright lost 42 posts.
    items = [{"title": f"By {a} {i}", "link": f"https://{a}/posts/{i}"}
             for i in range(2) for a in ("author1.net", "author2.net", "author3.net")]
    own1 = [{"title": f"By author1.net {i}", "link": f"https://author1.net/posts/{i}"}
            for i in range(2)]
    r = build("planettest.org", "https://planettest.org", "https://planettest.org/planet.xml",
              "https://planettest.org/planet.xml", items,
              others=[("author1.net", "https://author1.net", own1),
                      ("author2.net", "https://author2.net")])
    check(all(LAST_HOMES.get(f"By author1.net {i}") == "https://author1.net" for i in range(2)),
          "a planet's entry that its author's feed indexes is the author's",
          str({t: h for t, h in LAST_HOMES.items() if "author1" in t}))
    check(sum(1 for t, h in LAST_HOMES.items()
              if t.startswith("By author2") and h == "https://planettest.org") == 2,
          "...one by an indexed blog that does not index it stays with the planet")
    check(sum(t.startswith("By author3") for t in r) == 2,
          "...and so does one by a blog outside the index")

    # The feed that carried a post its author took gets the slot back, whatever
    # the order of feeds.jsonl. Holding it left ocaml.org with 8 posts in one
    # order and 12 in the other.
    planet = [{"title": "P1 by author1", "link": "https://author1.net/posts/1"},
              {"title": "O1", "link": "https://outside1.net/a"},
              {"title": "O2", "link": "https://outside2.net/b"},
              {"title": "O3", "link": "https://outside3.net/c"}]
    own = [{"title": "P1 by author1", "link": "https://author1.net/posts/1"}]
    kept = {}
    real_cap = build_index.FEED_CAP
    build_index.FEED_CAP = 2
    try:
        for first in (False, True):
            build("planettest.org", "https://planettest.org", "https://planettest.org/planet.xml",
                  "https://planettest.org/planet.xml", planet,
                  others=[("author1.net", "https://author1.net", own)], others_first=first)
            kept[first] = sorted(t for t, h in LAST_HOMES.items() if h == "https://planettest.org")
    finally:
        build_index.FEED_CAP = real_cap
    check(kept[False] == kept[True] == ["O1", "O2"],
          "a planet keeps the same posts whichever feed comes first", str(kept))
    check(LAST_HOMES.get("P1 by author1") == "https://author1.net", "...and the author keeps its own")

    # A cached record fetched from a feed the seed no longer names is dropped at
    # compaction, so the key is refetched from the current seed. Kept, the record
    # from medium.com/@steve.yegge's old feed -- now a spam account's -- shipped.
    d = tempfile.mkdtemp(prefix="feedlinks-")
    env = dict(os.environ)
    try:
        seed = os.path.join(d, "seed.tsv")
        with open(seed, "w") as f:
            f.write("moved.example\thttps://new.example/feed\nsame.example\thttps://same.example/feed\n")
        cache = os.path.join(d, "feeds.jsonl")
        with open(cache, "w") as f:
            for k, u in (("moved.example", "https://old.example/feed"),
                         ("same.example", "https://same.example/feed")):
                f.write(json.dumps({"key": k, "feed": u, "entries": [{"title": "t", "url": u}],
                                    "fetched_at": NOW, "v": 2}) + "\n")
        targets = os.path.join(d, "targets.jsonl")
        open(targets, "w").close()
        os.environ["FEED_URLS"] = seed
        sys.argv = ["fetch_feeds.py", targets, cache, "1"]
        with contextlib.redirect_stdout(io.StringIO()):
            fetch_feeds.main()
        left = [json.loads(l)["key"] for l in open(cache) if l.strip()]
        check(left == ["same.example"], "a cached record from a feed the seed no longer names is dropped",
              str(left))
    finally:
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(d)

    print(f"\n{len(failures)} FAILURE(S)" if failures else "\nall feed-link checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
