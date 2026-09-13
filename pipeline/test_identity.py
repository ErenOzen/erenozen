#!/usr/bin/env python3
"""Which blog a URL belongs to: the identity rules, on crafted inputs.

Each case is a blog the rules in aggregate_domains.py dropped, merged with
strangers, split into pieces or indexed as something it is not, before a rule
was added. The last two run the real aggregation and the real build, because
an identity merged in one and ignored in the other loses every story on it.
"""
import contextlib, io, json, os, shutil, struct, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aggregate_domains as agg
import build_index

failures = []


def check(ok, what, detail=""):
    print(("  ok   " if ok else "  FAIL ") + what + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(what)


def key(url):
    k = agg.blog_key(url)
    return k[0] if k else None


URLS = [
    # A fediverse status is a social post; self-hosted instances were indexed
    # as blogs of other people's toots.
    ("https://hachyderm.io/@robpike/112345678901234567", None,
     "a Mastodon status is not a blog post"),
    ("https://example.social/users/alice/statuses/109876543210987654", None,
     "...nor is one in the /users/<name>/statuses form"),
    ("https://dotat.at/@/2024-01-01-foo.html", "dotat.at",
     "a personal site's /@/ page still is"),
    ("https://medium.com/@bellmar/why-i-wrote-3a9f1c2b7d4e", "medium.com/@bellmar",
     "a Medium post keeps its @author"),
    # Buttondown moved to .com and merged every newsletter into one blog.
    ("https://buttondown.com/hillelwayne/archive/a-crash-course/", "buttondown.com/hillelwayne",
     "buttondown.com splits by newsletter"),
    # A platform's own /blog was skipped as a generic route.
    ("https://buttondown.com/blog/what-i-love-about-django", "buttondown.com/blog",
     "a platform's own /blog is one blog"),
    ("https://www.notion.so/blog/sqlite-wasm", "notion.so/blog",
     "...Notion's engineering blog included"),
    # Path platforms that also give authors subdomains: every post was its own
    # one-story blog, or the blog was dropped outright.
    ("https://doctorow.medium.com/a-denialism-taxonomy-eeb1a2766849", "doctorow.medium.com",
     "a Medium subdomain is one blog, not one per post"),
    ("https://jwz.livejournal.com/1057786.html", "jwz.livejournal.com",
     "a LiveJournal is one blog"),
    ("https://ludic.mataroa.blog/blog/i-accidentally-saved/", "ludic.mataroa.blog",
     "a Mataroa blog is its subdomain"),
    ("https://opensourcewatch.beehiiv.com/p/some-post", "opensourcewatch.beehiiv.com",
     "a beehiiv newsletter is its subdomain"),
    ("https://uecker.codeberg.page/2025-07-20.html", "uecker.codeberg.page",
     "a codeberg.page site is its subdomain"),
    ("https://world.hey.com/dhh/some-post-1a2b3c", "world.hey.com/dhh",
     "HEY World still splits by author path"),
    ("https://medium.com/p/abcdef", None, "a Medium short link names no author"),
    # Other fediverse servers: social.kernel.org (Akkoma) stayed indexed.
    ("https://social.kernel.org/notice/B2JlhcxNTfI8oDVoyO", None,
     "an Akkoma status is not a blog post"),
    ("https://social.belkadan.com/@jrose/statuses/01HNRNHBQY4E5MC37KG14R50P7", None,
     "...nor a GoToSocial one"),
    ("https://couchdb.apache.org/notice/1.0.1.html", "couchdb.apache.org",
     "a site's own /notice/ page still counts"),
    ("https://medium.com/@user/123456789012", "medium.com/@user",
     "a slugless Medium post with an all-digit id is still a post"),
    # tumblr.com/blog/view/<name> is <name>'s blog, not Tumblr's own /blog.
    ("https://www.tumblr.com/blog/view/staff/123456", "staff.tumblr.com",
     "a Tumblr dashboard view is that blog"),
]


def main():
    for url, want, what in URLS:
        got = key(url)
        check(got == want, what, f"{got}")

    d = tempfile.mkdtemp(prefix="identity-")
    try:
        now = int(time.time())
        stories = []
        for i, (url, title, *pts) in enumerate([
                ("https://bellmar.medium.com/a-post-1a2b3c4d", "Bellotti on the subdomain A"),
                ("https://bellmar.medium.com/b-post-4d5e6f7a", "Bellotti on the subdomain B"),
                ("https://medium.com/@bellmar/c-post-7a8b9c0d", "Bellotti on the old address"),
                # Post A again, at the other address and with fewer points.
                ("https://medium.com/@bellmar/a-post-1a2b3c4d", "Bellotti post A again"),
                ("https://doctorow.medium.com/d-post-1f2e3d4c", "Doctorow one"),
                ("https://doctorow.medium.com/e-post-5b6a7c8d", "Doctorow two"),
                ("https://doctorow.medium.com/f-post-9e0f1a2b", "Doctorow three"),
                ("https://buttondown.com/hillelwayne/archive/g/", "Wayne on the new domain G"),
                ("https://buttondown.com/hillelwayne/archive/h/", "Wayne on the new domain H"),
                ("https://buttondown.email/hillelwayne/archive/i/", "Wayne on the old domain"),
                # @steve.yegge writes at steve-yegge.medium.com.
                ("https://steve-yegge.medium.com/j-post-1a1a1a1a", "Yegge on the subdomain J"),
                ("https://steve-yegge.medium.com/k-post-2b2b2b2b", "Yegge on the subdomain K"),
                ("https://medium.com/@steve.yegge/l-post-3c3c3c3c", "Yegge on the old handle"),
                # Two handles spell a-b; the subdomain is neither's for sure.
                ("https://medium.com/@a.b/m-post-4d4d4d4d", "A dot B"),
                ("https://medium.com/@a_b/n-post-5e5e5e5e", "A underscore B"),
                ("https://a-b.medium.com/o-post-6f6f6f6f", "A-B one"),
                ("https://a-b.medium.com/p-post-7a7a7a7a", "A-B two"),
                ("https://a-b.medium.com/q-post-8b8b8b8b", "A-B three"),
                # Labelled only under the subdomain, like glaubercosta-11125.
                ("https://glauber-test.medium.com/r-post-9c9c9c9c", "Glauber on the subdomain R"),
                ("https://glauber-test.medium.com/s-post-0d0d0d0d", "Glauber on the subdomain S"),
                ("https://medium.com/@glauber_test/t-post-1e1e1e1e", "Glauber on the old handle"),
                # Buttondown's own blog, hidden as a vendor: a story under the
                # 150-point bar for hidden sources is not indexed.
                ("https://buttondown.email/blog/x1-post", "Buttondown own post 1", 200),
                ("https://buttondown.email/blog/x2-post", "Buttondown own post 2", 200),
                ("https://buttondown.com/blog/love-django", "Love Django on HN", 134)]):
            stories.append({"objectID": str(1000 + i), "url": url, "title": title,
                            "points": pts[0] if pts else (50 if "again" in title else 100),
                            "created_at_i": now - i * 86400})
        src = os.path.join(d, "stories.jsonl")
        with open(src, "w") as f:
            for s in stories:
                f.write(json.dumps(s) + "\n")
        cand = os.path.join(d, "cands.jsonl")
        sys.argv = ["aggregate_domains.py", src, cand, "3"]
        with contextlib.redirect_stdout(io.StringIO()):
            agg.main()
        rows = {r["key"]: r for r in map(json.loads, open(cand))}
        b = rows.get("medium.com/@bellmar", {})
        check(b.get("n_stories") == 4 and "bellmar.medium.com" not in rows,
              "a Medium author's two addresses are one blog", f"{sorted(rows)}")
        check(b.get("aliases") == ["bellmar.medium.com"],
              "...with the subdomain recorded as its alias", f"{b.get('aliases')}")
        check(rows.get("doctorow.medium.com", {}).get("n_stories") == 3,
              "a subdomain with no older twin is a blog of its own")
        hw = rows.get("buttondown.email/hillelwayne", {})
        check(hw.get("n_stories") == 3 and "buttondown.com/hillelwayne" not in rows
              and hw.get("aliases") == ["buttondown.com/hillelwayne"],
              "a newsletter on Buttondown's old and new domains is one blog",
              f"{hw.get('n_stories')} stories, aliases {hw.get('aliases')}")
        y = rows.get("medium.com/@steve.yegge", {})
        check(y.get("n_stories") == 3 and "steve-yegge.medium.com" not in rows,
              "a Medium handle with a dot merges with its dashed subdomain", f"{sorted(rows)}")
        check(y.get("home") == "https://steve-yegge.medium.com",
              "...and the merged home is the subdomain, not the re-registrable @handle",
              f"{y.get('home')}")
        check(rows.get("a-b.medium.com", {}).get("n_stories") == 3,
              "a subdomain whose spelling fits two handles merges with neither")

        # The build must follow the alias, or both subdomain stories vanish. It
        # also gets a planet's feed and a feed for Buttondown's own blog.
        with open(cand, "a") as f:
            f.write(json.dumps({"key": "planettest.org", "home": "https://planettest.org",
                                "n_stories": 5, "median_points": 50, "last_seen": now}) + "\n")
        cls = os.path.join(d, "cls")
        os.makedirs(cls)
        with open(os.path.join(cls, "t.jsonl"), "w") as f:
            # The Glauber blog is labelled under its alias only.
            for k in [k for k in rows if k != "medium.com/@glauber_test"] + [
                    "glauber-test.medium.com", "planettest.org"]:
                f.write(json.dumps({"key": k, "is_programming_blog": True,
                                    "source": "vendor" if k == "buttondown.email/blog" else "personal",
                                    "topics": [{"slug": "web", "weight": 1.0}]}) + "\n")

        def entry(title, url, i):
            return {"title": title, "url": url, "published": now - i * 2 * 86400,
                    "summary": "", "alt": []}

        feeds = os.path.join(d, "feeds.jsonl")
        with open(feeds, "w") as f:
            for key_, home_, ents_ in [
                    ("buttondown.email/blog", "https://buttondown.email/blog",
                     [entry("Love Django (feed)", "https://buttondown.com/blog/love-django", 1)]),
                    ("planettest.org", "https://planettest.org",
                     [entry("Planet: Bellotti A", "https://medium.com/@bellmar/a-post-1a2b3c4d", 1)]
                     + [entry(f"Planet: outside {j}", f"https://x{j}.org/p", j + 1) for j in (1, 2, 3)])]:
                f.write(json.dumps({"key": key_, "home": home_, "feed": home_ + "/feed",
                                    "feed_final": home_ + "/feed", "entries": ents_,
                                    "fetched_at": now, "v": 2}) + "\n")
        out = os.path.join(d, "out")
        sys.argv = ["build_index.py", src, cand, cls, out, feeds]
        with contextlib.redirect_stdout(io.StringIO()):
            build_index.main()
        blogs = json.load(open(os.path.join(out, "blogs.json")))
        n = json.load(open(os.path.join(out, "meta.json")))["n_posts"]
        bid = struct.unpack_from(f"<{n}I", open(os.path.join(out, "posts.bin"), "rb").read(), 0)
        titles = open(os.path.join(out, "titles.txt"), encoding="utf-8").read().split("\n")[:n]
        per = {}
        for i in range(n):
            per[blogs[bid[i]]["h"]] = per.get(blogs[bid[i]]["h"], 0) + 1
        check("Love Django (feed)" in titles,
              "a hidden blog's feed copy survives when its HN twin is under the bar")
        check(titles.count("Bellotti on the subdomain A") == 1 and "Planet: Bellotti A" not in titles,
              "a planet's link to a merged blog's post at its other address is left to that blog")
        check(sum(t.startswith("Planet: outside") for t in titles) == 3,
              "...and the planet keeps its own finds")
        check(per.get("https://bellmar.medium.com") == 3,
              "the build indexes the alias's stories under the merged blog, "
              "one post submitted at both addresses once", f"{per}")
        check(per.get("https://steve-yegge.medium.com") == 3,
              "...the dotted handle's stories under its merged blog")
        check(per.get("https://glauber-test.medium.com") == 3,
              "a merged blog labelled only under its alias stays in the index")
        check(per.get("https://doctorow.medium.com") == 3, "...and the lone subdomain's under its own")
        check(per.get("https://buttondown.email/hillelwayne") == 3,
              "...and both domains' newsletter issues under one blog")
    finally:
        shutil.rmtree(d)

    print(f"\n{len(failures)} FAILURE(S)" if failures else "\nall identity checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
