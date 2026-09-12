#!/usr/bin/env python3
"""Fail loudly if the built index is internally inconsistent.

This runs in CI before the index is committed. Every check here corresponds to a
way the index can be silently wrong -- silently, because the UI would still load
and search would still return rows; they would just be the wrong rows, or point
at the wrong URLs. A column that is off by one is invisible until a user clicks a
result and lands on an unrelated post.
"""
import json, os, re, struct, sys

# Recall canaries. The platform-hosted entries are deliberate: substack and
# wordpress blogs were once dropped entirely (a substack URL is
# foo.substack.com/p/slug, and "p" was treated as a generic route segment) or
# fragmented into date archives. Both failures were silent -- the index built
# fine, it was just missing Terence Tao and Michal Zalewski.
FAMOUS = [
    "danluu.com", "jvns.ca", "simonwillison.net", "rachelbythebay.com",
    "lwn.net", "blog.cloudflare.com", "antirez.com", "nullprogram.com",
    "randomascii.wordpress.com", "lcamtuf.substack.com",
    "fgiesen.wordpress.com", "0fps.wordpress.com",
]
# Deliberately NOT canaries, though the same key fix recovered them:
#   terrytao.wordpress.com   -- classifies science:1.0, no software topic. A
#                               mathematics blog; correctly out of a PROGRAMMING
#                               blog finder. This canary fired once and the
#                               pipeline turned out to be right.
#   astralcodexten.substack.com -- society, is_programming_blog=false.

# Golden (HN item id -> title) pairs, verified against the live Algolia API.
# The index is four files that must agree row-for-row: titles.txt, paths.txt,
# posts.bin and hn.bin. Nothing inside the index can detect a permutation bug
# between them -- every file stays individually well-formed, every count still
# matches, and the UI happily renders a real title next to another post's HN
# thread and a third post's URL. These pairs are the outside reference.
# build_index.py now reorders every column by score, which is exactly the kind
# of change that breaks this silently.
GOLDEN = {
    34170379: "A day in the life of almost every vending machine",
    14172253: "The U.S. wind industry now employs more than 100K people",
    22061174: "Quindar Tones: the beeps heard in recordings of astronauts in space",
    28371203: "My House",
    5638914: "How I write SQL",
    10163075: "Safe from what?",
    43513967: "How IMAP works under the hood",
    27421202: "Jeff Bezos will fly on the first passenger spaceflight of Blue Origin in July",
}


def main():
    d = sys.argv[1]
    fail = []
    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg)
        if not ok:
            fail.append(msg)

    meta = json.load(open(os.path.join(d, "meta.json")))
    blogs = json.load(open(os.path.join(d, "blogs.json")))
    n, nb = meta["n_posts"], meta["n_blogs"]
    print(f"index: {n:,} posts / {nb:,} blogs")

    check(n > 10_000, f"post count plausible ({n:,} > 10,000)")
    check(nb > 500, f"blog count plausible ({nb:,} > 500)")
    check(len(blogs) == nb, f"blogs.json rows == meta.n_blogs ({len(blogs)} vs {nb})")
    dotted = [b["n"] for b in blogs if b["n"].split("/")[0].endswith(".")]
    check(not dotted, "no blog host ends in a dot" + (f" (e.g. {dotted[0]})" if dotted else ""))

    with open(os.path.join(d, "titles.txt"), encoding="utf-8") as f:
        titles = f.read().split("\n")
    with open(os.path.join(d, "paths.txt"), encoding="utf-8") as f:
        paths = f.read().split("\n")
    check(len(titles) == n, f"titles.txt lines == n_posts ({len(titles)} vs {n})")
    check(len(paths) == n, f"paths.txt lines == n_posts ({len(paths)} vs {n})")
    check(all(t.strip() for t in titles), "no empty titles")

    # Every path is rooted ("/x", relative to the blog home) or a full URL (a
    # post on another host). Anything else is concatenated straight onto the
    # home -- 99 rows shipped as https://mchav.github.iomchav.github.io/...
    # ...and a full URL must be on a public host a reader can load. Checking label
    # syntax was not enough: "localhost", "tinyclouds" and "ai.html" are all
    # well-formed, and the first version of this check passed 69 such URLs --
    # links to the reader's own machine among them.
    import ipaddress
    from urllib.parse import urlparse as _up
    from publicsuffix2 import get_sld
    _label = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
    _reserved = (".local", ".localhost", ".test", ".example", ".invalid", ".internal",
                 ".lan", ".home.arpa")

    def _loadable(u):
        try:
            h = (_up(u).hostname or "").rstrip(".")
            h = h.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return False
        if not h or h == "localhost" or h.endswith(_reserved):
            return False
        if not all(_label.match(x) for x in h.split(".")):
            return False
        try:
            return ipaddress.ip_address(h).is_global
        except ValueError:
            return get_sld(h, strict=True) is not None
    unloadable = [x for x in paths if x.startswith(("http://", "https://")) and not _loadable(x)]
    check(not unloadable, "every full URL is on a public host a reader can load" +
          (f" ({len(unloadable)} are not, e.g. {unloadable[0][:50]!r})" if unloadable else ""))
    neither = [x for x in paths if not x.startswith(("/", "http://", "https://"))]
    check(not neither, "every path is rooted or a full URL" +
          (f" ({len(neither)} are neither, e.g. {neither[0][:40]!r})" if neither else ""))

    # Titles render through textContent, so an undecoded entity is shown to the
    # reader verbatim: "Embellishing the donut&#58;", "A story about &lt;input&gt;".
    # Both HN and feed titles arrive encoded, so this has to be decoded at build.
    ENT = re.compile(r"&(#[0-9]{2,5}|#x[0-9a-fA-F]{2,4}|"
                     r"lt|gt|amp|quot|apos|nbsp|mdash|ndash|hellip|[lr][sd]quo);")
    enc = [t for t in titles if ENT.search(t)]
    check(not enc, "titles are entity-decoded" +
          (f" ({len(enc)} still encoded, e.g. {enc[0][:40]!r})" if enc else ""))

    size = os.path.getsize(os.path.join(d, "posts.bin"))
    check(size == n * 12, f"posts.bin is n*12 bytes ({size} vs {n*12})")
    hsize = os.path.getsize(os.path.join(d, "hn.bin"))
    check(hsize == n * 4, f"hn.bin is n*4 bytes ({hsize} vs {n*4})")

    with open(os.path.join(d, "posts.bin"), "rb") as f:
        buf = f.read()
    with open(os.path.join(d, "hn.bin"), "rb") as f:
        hbuf = f.read()
    blog_ids = struct.unpack_from(f"<{n}I", buf, 0)
    pts = struct.unpack_from(f"<{n}H", buf, n * 4)
    tm = struct.unpack_from(f"<{n}H", buf, n * 8)
    ks = struct.unpack_from(f"<{n}B", buf, n * 10)
    hn = struct.unpack_from(f"<{n}I", hbuf, 0)

    check(max(blog_ids) < nb, f"all blogId in range (max {max(blog_ids)} < {nb})")

    # Path-platform blogs keep the author in the home (medium.com/@bellmar). A
    # stored path that starts with that segment renders it twice -- 1,525 posts
    # linked to medium.com/@bellmar/@bellmar/... and 42.9% of them were badged
    # dead because the crawler probed the same wrong URL.
    from urllib.parse import urlparse
    hp = [urlparse(b["h"]).path.rstrip("/") for b in blogs]
    hh = [urlparse(b["h"]).netloc.lower().removeprefix("www.") for b in blogs]
    dbl = sum(1 for i in range(n)
              if hp[blog_ids[i]] and paths[i].startswith(hp[blog_ids[i]] + "/"))
    check(dbl == 0, f"no path repeats its blog's home path ({dbl} do)")
    # A full URL on the blog's own host means post_path failed to relativize it.
    own = sum(1 for i in range(n) if paths[i].startswith(("http://", "https://"))
              and urlparse(paths[i]).netloc.lower().removeprefix("www.") == hh[blog_ids[i]]
              and not hp[blog_ids[i]])
    check(own == 0, f"full URLs only for posts on another host ({own} are on their own)")
    # Feed-sourced posts carry no HN score, so the bar applies to HN posts only.
    hn_pts = [p for p, t in zip(pts, tm) if not (t & (1 << 13))]
    check(min(hn_pts) >= 25,
          f"all HN posts clear the 25-point bar (min {min(hn_pts)})")
    n_feed = sum(1 for t in tm if t & (1 << 13))
    check(n_feed > 0, f"feed posts present ({n_feed:,})")
    check(all(p == 0 for p, t in zip(pts, tm) if t & (1 << 13)),
          "feed posts carry no fabricated HN score")

    # The per-blog feed cap is the only thing stopping one high-cadence
    # publisher from owning the Newest view. It used to be applied per RECORD,
    # so a blog refetched by the monthly refresh could contribute twice its
    # share -- with the cap still printed in the build log as if it held.
    from collections import Counter
    per_blog = Counter(b for b, t in zip(blog_ids, tm) if t & (1 << 13))
    worst = max(per_blog.values()) if per_blog else 0
    check(worst <= 12, f"no blog exceeds the feed cap (worst {worst})")
    check(max((t & 0x0FFF).bit_length() for t in tm) <= 12, "topic mask within 12 topics")
    n_rule = sum(1 for t in tm if t & (1 << 14))
    check(0.10 < n_rule / n < 0.60,
          f"kind rules fire on a sane share ({n_rule/n:.1%} rule-derived)")
    check(max(k >> 3 for k in ks) < len(meta["sources"]), "source index in range")
    check(max(k & 7 for k in ks) < len(meta["kinds"]), "kind index in range")
    check(sum(1 for t in tm if (t & 0x0FFF) == 0) < n * 0.05,
          f"under 5% of posts have no topic ({sum(1 for t in tm if (t&0x0FFF)==0)/n:.1%})")

    missing_hn = sum(1 for h, t in zip(hn, tm) if h == 0 and not (t & (1 << 13)))
    check(missing_hn < n * 0.01, f"HN ids present on HN posts ({missing_hn} missing)")

    pos = {h: i for i, h in enumerate(hn) if h}
    bad = [f"{h} -> {titles[pos[h]]!r}" for h, t in GOLDEN.items()
           if h in pos and titles[pos[h]] != t]
    gone = [h for h in GOLDEN if h not in pos]
    check(not bad, "hn.bin still lines up with titles.txt" +
          (f" (mismatched: {bad[0]})" if bad else ""))
    check(len(gone) <= 2, f"golden posts still indexed ({len(gone)} of "
                          f"{len(GOLDEN)} missing)")

    names = {b["n"] for b in blogs}
    missing = [f for f in FAMOUS if f not in names]
    check(not missing, f"famous blogs present (missing: {missing or 'none'})")

    # Dead-link flags. Both directions matter: zero means the link-check
    # argument was quietly dropped from the build command (the index still
    # builds fine, it just stops warning anyone), and a huge share means the
    # crawler was blocked wholesale rather than finding real rot. Measured at
    # 12.0% over 154k URLs, decaying monotonically from 32% (2009) to 4% (2025).
    n_dead = sum(1 for t in tm if t & (1 << 15))
    check(0.02 < n_dead / n < 0.30,
          f"dead-link share sane ({n_dead/n:.1%} of posts flagged)")

    # The newsroom toggle is the product's main promise; if it hides nothing or
    # everything, something upstream broke.
    hidden = meta["hidden_source_mask"]
    nh = sum(1 for k in ks if (hidden >> (k >> 3)) & 1)
    check(0.10 < nh / n < 0.85, f"hidden-source share sane ({nh/n:.1%} of posts)")

    # Regression guard.
    #
    # Every other check here asks whether the index is internally consistent.
    # All of them pass on an index that is consistent and much smaller than the
    # one it replaces -- which is exactly what a scheduled refresh produces when
    # an input goes missing. Measured: a cold feed cache builds a perfectly
    # valid index with 17.9% fewer posts and 3,731 fewer subscribe links, and
    # the old suite waved it through.
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        prev = json.load(open(sys.argv[2]))
        print(f"\nbaseline: {prev.get('n_posts', 0):,} posts / "
              f"{prev.get('n_blogs', 0):,} blogs")
        for key, label, tol in (("n_posts", "posts", 0.05),
                                ("n_blogs", "blogs", 0.05),
                                ("n_feed_urls", "feed URLs", 0.10),
                                ("n_feed_posts", "feed posts", 0.10)):
            before = prev.get(key)
            if not before:
                continue          # baseline predates this field
            now_v = meta.get(key, 0)
            drop = (before - now_v) / before
            check(drop <= tol,
                  f"{label} did not regress ({before:,} -> {now_v:,}, "
                  f"{-drop:+.1%}, tolerance -{tol:.0%})")

    print()
    if fail:
        print(f"{len(fail)} CHECK(S) FAILED")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
