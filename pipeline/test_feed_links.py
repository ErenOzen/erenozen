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


def build(key, home, feed, final, items):
    """Index one personal blog from a crafted feed; return {title: post URL}."""
    d = tempfile.mkdtemp(prefix="feedlinks-")
    try:
        cls = os.path.join(d, "cls")
        os.makedirs(cls)
        fetch_feeds._get = lambda url, **kw: _Resp(rss(items), final)
        entries, _, fin = fetch_feeds.parse_feed(feed)
        with open(os.path.join(d, "cands.jsonl"), "w") as f:
            f.write(json.dumps({"key": key, "home": home, "n_stories": 5,
                                "median_points": 50, "last_seen": NOW}) + "\n")
        with open(os.path.join(cls, "t.jsonl"), "w") as f:
            f.write(json.dumps({"key": key, "source": "personal", "is_programming_blog": True,
                                "topics": [{"slug": "web", "weight": 1.0}]}) + "\n")
        with open(os.path.join(d, "feeds.jsonl"), "w") as f:
            f.write(json.dumps({"key": key, "home": home, "feed": feed, "feed_final": fin,
                                "entries": entries, "fetched_at": NOW, "v": 2}) + "\n")
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

    print(f"\n{len(failures)} FAILURE(S)" if failures else "\nall feed-link checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
