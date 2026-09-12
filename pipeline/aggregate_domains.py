#!/usr/bin/env python3
"""Aggregate HN stories into candidate blogs.

HN ranks posts; we want blogs. Group stories by *blog identity* -- which is
usually the registrable domain, but is domain+path on platforms that host many
authors under one domain (medium.com/@user, dev.to/user).

Ranks by number of DISTINCT stories that cleared the points bar, not by total
points, so one viral post can't promote a blog nobody reads otherwise.
"""
import json, re, sys, statistics
from collections import defaultdict
from urllib.parse import urlparse
from publicsuffix2 import get_sld

# NB: parsed inside main(), not at import time -- build_index.py imports
# blog_key() from this module and must not inherit its CLI contract.

# Hosts where one domain fronts many independent authors -> identity includes
# the first path segment.
PATH_PLATFORMS = {
    "medium.com", "dev.to", "hashnode.com", "hackernoon.com", "telegra.ph",
    "substack.com", "notion.site", "gitbook.io", "readthedocs.io",
    "blogspot.com", "livejournal.com", "wordpress.com", "tumblr.com",
    # Newsletter/site hosts where the author is the first path segment. Without
    # these the host collapses into one fake mega-blog (buttondown.email alone
    # merged 90 unrelated newsletters).
    "buttondown.email", "world.hey.com", "hey.com", "notion.so", "write.as",
    "tinyletter.com", "mataroa.blog", "beehiiv.com", "ghost.io", "omg.lol",
    "neocities.org", "codeberg.page", "srht.site",
}
# Hosts where the subdomain is the author -> keep the full hostname.
SUBDOMAIN_PLATFORMS = {
    "substack.com", "wordpress.com", "tumblr.com", "ghost.io", "bearblog.dev",
    "hashnode.dev", "svbtle.com", "posthaven.com", "micro.blog", "notion.site",
    "hatenablog.com", "hatenadiary.jp", "netlify.app", "vercel.app",
    "pages.dev", "surge.sh", "neocities.org", "gitbook.io", "webflow.io",
    "onrender.com", "fly.dev", "workers.dev", "glitch.me", "repl.co",
}

# Unambiguous non-blogs only. Anything requiring judgment (corporate eng blogs,
# tech journalism) is deliberately left in for the classifier.
DENY_EXACT = {
    # social / aggregators / forums
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
    "reddit.com", "old.reddit.com", "news.ycombinator.com", "lobste.rs",
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "tiktok.com",
    "bsky.app", "threads.net", "mastodon.social", "pinterest.com",
    "quora.com", "stackoverflow.com", "stackexchange.com", "superuser.com",
    "serverfault.com", "askubuntu.com", "discord.com", "t.me", "imgur.com",
    "producthunt.com", "indiehackers.com", "slashdot.org", "digg.com",
    # code hosts / package registries
    "github.com", "gist.github.com", "gitlab.com", "bitbucket.org",
    "sourceforge.net", "codeberg.org", "sr.ht", "git.sr.ht", "npmjs.com",
    "pypi.org", "crates.io", "rubygems.org", "packagist.org", "nuget.org",
    "hub.docker.com", "dockerhub.com", "codepen.io", "jsfiddle.net",
    "replit.com", "observablehq.com", "kaggle.com", "huggingface.co",
    # reference / standards
    "wikipedia.org", "en.wikipedia.org", "wikimedia.org", "wiktionary.org",
    "developer.mozilla.org", "w3.org", "ietf.org", "rfc-editor.org",
    "iso.org", "unicode.org", "khronos.org", "ecma-international.org",
    "archive.org", "web.archive.org", "wikidata.org",
    # academic repositories / publishers
    "arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "jstor.org",
    "sciencedirect.com", "springer.com", "link.springer.com", "nature.com",
    "science.org", "pnas.org", "plos.org", "journals.plos.org", "ieee.org",
    "ieeexplore.ieee.org", "dl.acm.org", "acm.org", "semanticscholar.org",
    "researchgate.net", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "papers.nips.cc", "proceedings.neurips.cc", "openreview.net",
    # commerce / app stores / misc
    "amazon.com", "ebay.com", "aliexpress.com", "apps.apple.com",
    "play.google.com", "store.steampowered.com", "docs.google.com",
    "drive.google.com", "groups.google.com", "patents.google.com",
    "books.google.com", "scholar.google.com", "goo.gl", "bit.ly",
}
DENY_SUFFIX = (".gov", ".mil", ".edu")
DENY_PATTERN = re.compile(
    r"(^|\.)(login|auth|accounts|checkout|shop|store|support|status)\.", re.I
)


def blog_key(url):
    """Return (key, home_url) identifying the blog, or None if unusable."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    # rstrip("."): a fully-qualified name keeps its root dot, and without this
    # homepage.ntlworld.com. was indexed as a second blog beside
    # homepage.ntlworld.com.
    host = p.netloc.lower().split(":")[0].rstrip(".").removeprefix("www.")
    if not host or "." not in host:
        return None

    sld = get_sld(host) or host

    # Subdomain check FIRST. Several hosts appear in both sets (wordpress.com,
    # tumblr.com, substack.com): the author is the subdomain when there is one,
    # and only the path when the URL sits on the platform root. Running the path
    # rule first split randomascii.wordpress.com into /2014, /2018 and /2022 --
    # 147 blogs fragmented into date archives, Terence Tao's among them.
    if sld in SUBDOMAIN_PLATFORMS and host != sld:
        return host, f"https://{host}"

    # Author lives in the path on these hosts.
    if sld in PATH_PLATFORMS or host in PATH_PLATFORMS:
        seg = [s for s in p.path.split("/") if s]
        if seg:
            first = seg[0]
            # medium.com/@user, dev.to/user -- but skip generic route segments
            # and date-archive paths, which are never an author.
            if (first.lower() not in {"p", "tag", "search", "feed", "s", "m",
                                      "post", "posts", "blog", "archive"}
                    and not re.fullmatch(r"(19|20)\d\d", first)):
                return f"{host}/{first}", f"https://{host}/{first}"
        return None  # bare platform root is not a blog

    # Otherwise the blog is the hostname (keeps blog.foo.com distinct from
    # foo.com, which matters: corporate eng blogs usually live on a subdomain).
    return host, f"https://{host}"


def denied(key):
    host = key.split("/")[0]
    sld = get_sld(host) or host
    if host in DENY_EXACT or sld in DENY_EXACT:
        return True
    if host.endswith(DENY_SUFFIX):
        return True
    if DENY_PATTERN.search(host):
        return True
    return False


def main():
    src, out = sys.argv[1], sys.argv[2]
    MIN_STORIES = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    blogs = defaultdict(lambda: {"stories": [], "home": None, "www": 0})
    total = skipped = 0

    with open(src) as f:
        for line in f:
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if not s.get("url") or not s.get("title"):
                skipped += 1
                continue
            k = blog_key(s["url"])
            if not k:
                skipped += 1
                continue
            key, home = k
            if denied(key):
                skipped += 1
                continue
            b = blogs[key]
            b["home"] = home
            b["stories"].append(s)
            if urlparse(s["url"]).netloc.lower().startswith("www."):
                b["www"] += 1

    rows = []
    for key, b in blogs.items():
        st = b["stories"]
        if len(st) < MIN_STORIES:
            continue
        pts = [s["points"] or 0 for s in st]
        yrs = [s["created_at_i"] for s in st]
        top = sorted(st, key=lambda s: -(s["points"] or 0))[:5]
        rows.append({
            "key": key,
            # The home keeps the host form the blog's own URLs use. blog_key
            # drops "www." so that a blog has one identity, but building every
            # link from the bare host broke 41 blogs whose bare domain has no
            # address at all -- stephendiehl.com and vim.org among them -- and
            # the crawler duly reported 303 of their posts dead. The key stays
            # www-free; only the URL handed to readers changes.
            "home": (f"https://www.{key}" if "/" not in key and b["www"] * 2 > len(st)
                     else b["home"]),
            "n_stories": len(st),
            "total_points": sum(pts),
            "median_points": int(statistics.median(pts)),
            "max_points": max(pts),
            "first_seen": min(yrs),
            "last_seen": max(yrs),
            "sample_titles": [t["title"] for t in top],
            "sample_urls": [t["url"] for t in top],
        })

    rows.sort(key=lambda r: (-r["n_stories"], -r["total_points"]))
    with open(out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"stories read      : {total}")
    print(f"skipped/denied    : {skipped}")
    print(f"distinct blog keys: {len(blogs)}")
    print(f"candidates (>={MIN_STORIES}) : {len(rows)}")


if __name__ == "__main__":
    main()
