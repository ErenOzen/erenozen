#!/usr/bin/env python3
"""Drive the blog finder in a real browser and assert it actually works.

Everything before this was static reasoning about code that had never been
rendered. This serves blogs/ over HTTP (the worker needs a real origin -- it
cannot load from file://) and exercises the paths a visitor takes.
"""
import http.server, json, os, re, socketserver, sys, tempfile, threading, time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent / "blogs"
PORT = 8731

failures = []

# Screenshots are diagnostics, not assertions; CI has nowhere to put them.
SHOTS = os.environ.get("SHOT_DIR") or tempfile.mkdtemp(prefix="blogfinder-shots-")

JS_CONTRAST = r'''() => {
                const lin = (c) => { c /= 255;
                    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
                const L = (n) => 0.2126*lin(n[0]) + 0.7152*lin(n[1]) + 0.0722*lin(n[2]);
                const bg = getComputedStyle(document.body).backgroundColor
                             .match(/[\d.]+/g).map(Number);
                // Flatten the element's own alpha onto what is behind it: a
                // dimmed colour is not the colour the reader actually sees.
                const eff = (el) => {
                    const cs = getComputedStyle(el);
                    // Hover-revealed links sit at opacity 0 until hovered;
                    // measure the state the reader actually sees them in.
                    const a = parseFloat(cs.opacity) || 1;
                    const fg = cs.color.match(/[\d.]+/g).map(Number);
                    const mix = fg.slice(0, 3).map((v, i) => v*a + bg[i]*(1-a));
                    const l1 = L(mix), l2 = L(bg);
                    return (Math.max(l1,l2) + 0.05) / (Math.min(l1,l2) + 0.05);
                };
                const out = {};
                for (const sel of [".opml", ".feed-link", ".sim-chip",
                                   ".pin-similar-label", ".status-ms", ".pin-desc",
                                   ".r-blog", ".r-pts", ".r-year", ".r-tag",
                                   ".r-desc", ".sort-label", ".toggle span",
                                   ".chip", ".reset", ".suggests button",
                                   ".r-dead", ".r-feed", ".hn-link", ".load-note"]) {
                    const el = document.querySelector(sel);
                    if (el) out[sel] = eff(el);
                }
                return out;
            }'''


# Chrome: an explicit CHROME_PATH wins, then a system install, then Playwright's
# bundled build. Hardcoding /usr/bin/google-chrome meant these tests could only
# ever run on the machine they were written on.
def launch(p):
    exe = os.environ.get("CHROME_PATH") or (
        "/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
    return p.chromium.launch(executable_path=exe, args=["--no-sandbox"])


def expected_source_counts():
    """Posts per source, read straight out of posts.bin.

    Ground truth the UI did not produce, so an assertion against it cannot be
    satisfied by a filter that does nothing."""
    import struct
    meta = json.loads((ROOT / "data/meta.json").read_text())
    n = meta["n_posts"]
    buf = (ROOT / "data/posts.bin").read_bytes()
    ks = struct.unpack_from(f"<{n}B", buf, n * 10)
    out = {s["name"]: 0 for s in meta["sources"]}
    names = [s["name"] for s in meta["sources"]]
    for k in ks:
        out[names[k >> 3]] += 1
    return out


def check(ok, label, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(f"{label} {detail}")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass


def serve():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    httpd = serve()
    base = f"http://127.0.0.1:{PORT}/"
    console, page_errors, failed_reqs = [], [], []

    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: console.append((m.type, m.text)))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed", lambda r: failed_reqs.append(r.url))

        t0 = time.time()
        page.goto(base, wait_until="load")
        print(f"\n== load ==  DOM ready in {time.time()-t0:.2f}s")

        # The input stays disabled until the worker signals ready.
        page.wait_for_function("() => !document.querySelector('#q').disabled",
                               timeout=60000)
        ready = time.time() - t0
        print(f"search ready in {ready:.2f}s")
        check(ready < 30, "index becomes searchable", f"{ready:.1f}s")

        check(not page_errors, "no uncaught page errors", "; ".join(page_errors[:2]))
        errs = [t for t in console if t[0] == "error"]
        check(not errs, "no console errors", "; ".join(t[1][:90] for t in errs[:2]))
        check(not failed_reqs, "no failed requests", "; ".join(failed_reqs[:2]))

        # --- default view ---
        page.wait_for_selector("#results li", timeout=20000)
        n = page.locator("#results li").count()
        check(n > 0, "default view renders results", f"{n} rows")
        print("  status:", page.locator("#status").inner_text())

        # --- typing ---
        page.fill("#q", "sqlite internals")
        page.wait_for_timeout(700)
        n = page.locator("#results li").count()
        first = page.locator("#results li").first.inner_text().replace("\n", " | ")
        check(n > 0, "query returns results", f"{n} rows")
        print("  top:", first[:104])

        # The multi-term fallback must be doing work here: "sqlite internals"
        # matches exactly one title strictly, and hundreds once the per-term
        # union kicks in. Parse the count -- this was a substring test for
        # "1 posts", which also matches "521 posts", so it failed the moment a
        # rebuilt index happened to return a count ending in 1.
        total = page.locator("#status").inner_text()
        m = re.search(r"([\d,]+)\s+posts", total)
        n_total = int(m.group(1).replace(",", "")) if m else 0
        check(n_total > 50, "multi-term fallback widens recall",
              f"{n_total} posts for a query with 1 strict match")

        # --- href correctness: the row must point at the real article ---
        href = page.locator("#results li a.row").first.get_attribute("href")
        check(bool(href) and href.startswith("http") and "undefined" not in href,
              "result links to a real URL", str(href)[:80])

        # --- HN discussion link ---
        hn = page.locator("#results li a.hn-link").first
        hn_href = hn.get_attribute("href") if hn.count() else None
        check(bool(hn_href) and "news.ycombinator.com/item?id=" in hn_href
              and not hn_href.endswith("id=0"), "HN discussion link is valid",
              str(hn_href)[:70])

        # --- nested anchors would be invalid HTML and break clicks ---
        nested = page.evaluate("() => document.querySelectorAll('a a').length")
        check(nested == 0, "no nested anchors", f"{nested} found")

        # --- sorting ---
        page.fill("#q", "")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(600)
        pts = page.evaluate("""() => [...document.querySelectorAll('#results .r-pts')]
            .map(e => parseInt(e.textContent.replace(/\\D/g,''))||0).slice(0,10)""")
        check(pts == sorted(pts, reverse=True), "sort by upvotes is descending", str(pts[:5]))
        check(pts and pts[0] > 1500, "top upvoted is genuinely top", str(pts[:1]))

        page.click('.sort-switch button[data-sort="date"]')
        page.wait_for_timeout(600)
        yrs = page.evaluate("""() => [...document.querySelectorAll('#results .r-meta')]
            .map(e => { const y = e.querySelector('.r-year'); return y?+y.textContent:0 })
            .filter(Boolean).slice(0,8)""")
        check(yrs == sorted(yrs, reverse=True), "sort by newest is descending", str(yrs[:5]))

        page.click('.sort-switch button[data-sort="oldest"]')
        page.wait_for_timeout(600)
        yrs_old = page.evaluate("""() => [...document.querySelectorAll('#results .r-meta')]
            .map(e => { const y = e.querySelector('.r-year'); return y?+y.textContent:0 })
            .filter(Boolean).slice(0,5)""")
        check(yrs_old and yrs_old[0] <= 2009, "sort by oldest reaches 2007-2009", str(yrs_old[:3]))

        # --- newsroom toggle ---
        page.click('.sort-switch button[data-sort="relevance"]')
        page.fill("#q", "chip")
        page.wait_for_timeout(700)
        before = page.locator("#status").inner_text()
        page.uncheck("#hide-news")
        page.wait_for_timeout(700)
        after = page.locator("#status").inner_text()
        nb = int(before.split()[0].replace(",", "")) if before else 0
        na = int(after.split()[0].replace(",", "")) if after else 0
        check(na > nb, "newsroom toggle widens the corpus", f"{nb} -> {na}")
        page.check("#hide-news")

        # --- topic facet ---
        page.fill("#q", "")
        page.wait_for_timeout(400)
        page.locator("#topics .chip").nth(5).click()   # security
        page.wait_for_timeout(700)
        n_sec = page.locator("#results li").count()
        check(n_sec > 0, "topic facet returns results", f"{n_sec} rows")
        page.locator("#topics .chip").nth(5).click()

        # --- blogs mode ---
        page.click('.mode-switch button[data-mode="blogs"]')
        page.wait_for_timeout(700)
        nb_rows = page.locator("#results li").count()
        first_blog = page.locator("#results li").first.inner_text().replace("\n", " | ")
        check(nb_rows > 0, "blogs mode renders", f"{nb_rows} rows")
        print("  top blog:", first_blog[:104])
        page.click('.mode-switch button[data-mode="posts"]')

        # --- REGRESSION: blogs-mode total must not equal the page size ---
        page.click('.mode-switch button[data-mode="blogs"]')
        page.fill("#q", "")
        page.wait_for_timeout(800)
        btxt = page.locator("#status").inner_text()
        btotal = int(btxt.split()[0].replace(",", "")) if btxt else 0
        brows = page.locator("#results > li").count()
        check(btotal > brows, "blogs mode reports the true total, not the page size",
              f"{brows} rows of {btotal}")
        check(not page.locator("#more").is_hidden(), "blogs mode offers Show more")
        page.click('.mode-switch button[data-mode="posts"]')
        page.wait_for_timeout(500)

        # --- REGRESSION: relevance must not collapse into popularity ---
        page.fill("#q", "rust")
        page.click('.sort-switch button[data-sort="relevance"]')
        page.wait_for_timeout(800)
        rel = page.evaluate(
            "() => [...document.querySelectorAll('#results > li .r-title')].slice(0,5).map(e=>e.textContent)")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(800)
        pop = page.evaluate(
            "() => [...document.querySelectorAll('#results > li .r-title')].slice(0,5).map(e=>e.textContent)")
        check(rel != pop, "relevance ordering differs from popularity ordering")
        on_topic = sum(1 for t in rel if "rust" in t.lower())
        check(on_topic >= 4, "relevance top 5 are on-topic for 'rust'", f"{on_topic}/5")
        page.click('.sort-switch button[data-sort="relevance"]')

        # --- contrast: matched text must not be the least legible thing on screen ---
        def lum(css_rgb):
            nums = [int(x) for x in css_rgb.replace("rgb(", "").replace(")", "").split(",")[:3]]
            def ch(c):
                c = c / 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = (ch(v) for v in nums)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        for theme in ("light", "dark"):
            if theme == "dark":
                page.click(".theme-toggle")
                page.wait_for_timeout(400)
            page.fill("#q", "rust")
            page.wait_for_timeout(700)
            cols = page.evaluate('''() => {
                const m = document.querySelector('#results > li .r-title mark');
                const body = getComputedStyle(document.body).backgroundColor;
                return m ? [getComputedStyle(m).color, body] : null;
            }''')
            if cols:
                l1, l2 = lum(cols[0]), lum(cols[1])
                ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
                check(ratio >= 4.5, f"matched-text contrast passes AA ({theme})",
                      f"{ratio:.2f}:1")
        page.click(".theme-toggle")
        page.wait_for_timeout(300)

        # --- contrast: every new control, in both themes ---
        #
        # Each of these was styled by eye against a cream background, where an
        # orange that looks fine is often 2.2:1. Opacity counts: .load-note and
        # .status-ms are dimmed, and dimming is exactly how small grey text
        # slides under 4.5:1 without anyone noticing.
        # Two pages, because no single view carries every style: the pin and
        # its recommendations only exist with a blog pinned, and .r-dead only
        # appears where the links have rotted.
        for theme in ("light", "dark"):
            ratios = {}
            for url in ("?b=jvns.ca", "?sort=oldest"):
                page.goto(base + url, wait_until="load")
                page.wait_for_function("() => !document.querySelector('#q').disabled",
                                       timeout=60000)
                page.wait_for_selector("#results > li", timeout=20000)
                page.wait_for_timeout(300)
                if theme == "dark" and page.evaluate(
                        "() => document.documentElement.dataset.theme !== 'dark'"):
                    page.click(".theme-toggle")
                    page.wait_for_timeout(400)
                for k, v in page.evaluate(JS_CONTRAST).items():
                    ratios[k] = min(ratios.get(k, 99), v)
            worst = min(ratios.items(), key=lambda kv: kv[1]) if ratios else None
            # The element count is in the message on purpose: a selector that
            # stops matching turns this into a check that passes by measuring
            # nothing, which is the failure mode it exists to prevent.
            check(worst is not None and len(ratios) >= 14 and worst[1] >= 4.5,
                  f"new controls pass AA ({theme})",
                  f"worst {worst[0]} {worst[1]:.2f}:1 of {len(ratios)} styles"
                  if worst else "no elements found")
        page.click(".theme-toggle")
        page.wait_for_timeout(300)

        # --- keyboard ---
        page.fill("#q", "rust")
        page.wait_for_timeout(600)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        sel = page.locator("#results li.sel").count()
        check(sel == 1, "ArrowDown selects a row", f"{sel} selected")

        # --- URL state round-trip ---
        page.fill("#q", "kernel")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(600)
        url = page.url
        check("q=kernel" in url and "sort=points" in url, "URL reflects state", url[-58:])
        page2 = browser.new_page()
        page2.goto(url, wait_until="load")
        page2.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page2.wait_for_timeout(900)
        check(page2.input_value("#q") == "kernel", "shared URL restores query")
        check(page2.locator('.sort-switch button[data-sort="points"].active').count() == 1,
              "shared URL restores sort")
        page2.close()

        # --- suggestion chips (empty state) ---
        # Explicit navigation, not just clearing the box: suggestions are hidden
        # while a blog is pinned, and an earlier block leaves one pinned. Tests
        # that inherit page state assert whatever the previous test happened to
        # leave behind.
        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.fill("#q", "")
        page.wait_for_timeout(500)
        check(page.locator("#suggests").is_visible(), "suggestions show on empty query")
        page.locator('#suggests button[data-q="sqlite internals"]').click()
        page.wait_for_timeout(800)
        check(page.input_value("#q") == "sqlite internals", "suggestion fills the query")
        check(not page.locator("#suggests").is_visible(),
              "suggestions hide once a query is active")

        # --- per-blog view ---
        page.fill("#q", "")
        page.wait_for_timeout(400)
        page.click('.mode-switch button[data-mode="blogs"]')
        page.wait_for_timeout(700)
        blog_name = page.locator("#results li .r-title").first.inner_text().strip()
        page.locator("#results li .blog-open").first.click()
        page.wait_for_timeout(800)
        check(page.locator("#pin").is_visible(), "pinning a blog shows its header")
        pin_text = page.locator("#pin .pin-name").inner_text().strip()
        check(pin_text == blog_name, "pinned blog matches the one clicked",
              f"{pin_text} vs {blog_name}")
        rows = page.locator("#results li").count()
        check(rows > 0, "pinned blog lists its posts", f"{rows} rows")
        shown = page.evaluate(
            "() => [...document.querySelectorAll('#results .r-blog')].map(e=>e.textContent)")
        check(len(set(shown)) == 1 and shown[0] == blog_name,
              "pinned view shows only that blog", str(set(shown))[:60])
        check("b=" in page.url, "pinned blog is in the URL", page.url[-46:])
        page.click("#pin .pin-clear")
        page.wait_for_timeout(700)
        page.wait_for_timeout(400)
        # "all blogs" has to mean all blogs. This only asserted the pin header
        # vanished, and passed the whole time the button was dumping the reader
        # into the full posts corpus instead.
        check(not page.locator("#pin").is_visible(), "clearing the pin hides the header")
        back = page.evaluate(
            "() => document.querySelector('.mode-switch .active').dataset.mode")
        check(back == "blogs", "clearing the pin returns to blogs, as the label says",
              f"landed in {back}")
        check(page.locator("#results li .blog-open").count() > 0,
              "blog rows are back after clearing the pin")

        # --- a11y: the meta line must not run numbers together ---
        # Posts mode explicitly: a blog row's meta has no points-or-year label,
        # so inheriting blogs mode from the block above turned this into a check
        # that failed for the wrong reason.
        page.goto(base + "?q=kernel", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(700)
        labels = page.evaluate('''() => {
            const m = document.querySelector('#results .r-meta');
            return [...m.children].map(c => c.getAttribute('aria-label') || c.textContent);
        }''')
        joined = " ".join(labels)
        check("points on Hacker News" in joined and "posted " in joined,
              "meta line exposes labelled points and year", joined[:80])

        # --- dark theme ---
        page.click(".theme-toggle")
        page.wait_for_timeout(400)
        theme = page.get_attribute("html", "data-theme")
        check(theme == "dark", "theme toggle works", str(theme))
        page.screenshot(path=os.path.join(SHOTS, "dark.png"))
        page.click(".theme-toggle")
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "light.png"))

        # --- dead links ---
        # 12% of the corpus 404s, concentrated in the oldest posts (32% of 2009
        # vs 4% of 2025). Sorting Oldest is exactly where a reader meets rot, so
        # that is where the warning has to be visible and the escape hatch real.
        page.goto(base + "?sort=oldest", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        n_dead = page.locator(".r-dead").count()
        check(n_dead > 0, "dead-link warning renders on the oldest posts",
              f"{n_dead} of {page.locator('#results > li').count()} rows")

        arc = page.locator("#results a.arc-link").first
        if arc.count():
            href = arc.get_attribute("href") or ""
            orig = page.locator("#results li").filter(
                has=page.locator("a.arc-link")).first.locator("a.row").get_attribute("href")
            check(href.startswith("https://web.archive.org/web/"),
                  "archive fallback points at the Wayback Machine", href[:60])
            check(orig and orig in href,
                  "archive link carries the original URL", (orig or "")[:60])
        else:
            check(False, "archive fallback link present on a dead row")

        # The warning must not leak onto live rows: every .r-dead needs a
        # sibling archive link, and rows without the tag must not have one.
        mismatch = page.evaluate("""() => {
            let bad = 0;
            for (const li of document.querySelectorAll('#results > li')) {
                const d = !!li.querySelector('.r-dead');
                const a = !!li.querySelector('a.arc-link');
                if (d !== a) bad++;
            }
            return bad;
        }""")
        check(mismatch == 0, "dead tag and archive link always travel together",
              f"{mismatch} mismatched rows")

        # --- source filter ---
        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(300)
        src_names = page.evaluate(
            "() => [...document.querySelectorAll('#sources .chip')].map(e => e.textContent)")
        check("Personal" in src_names, "source chips render", ", ".join(src_names))
        # "Institution" is in the taxonomy and classifies nothing; a chip that
        # can only ever return zero results is worse than no chip.
        check("Institution" not in src_names,
              "empty source classes get no chip", ", ".join(src_names))

        # Assert the exact post count for each source, computed from the index
        # itself. The first version of this block checked "no Newsroom tag is
        # visible" and "the count is > 0", and BOTH pass with source filtering
        # entirely disabled: postRow only renders a source tag for sources
        # hidden by default, and a no-op filter falls through to hideNews and
        # returns the whole 144k corpus, which is still greater than zero. It
        # printed the opposite of what it claimed and passed.
        want = expected_source_counts()
        total_shown = lambda: page.evaluate(
            "() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")

        page.click('#sources .chip:has-text("Personal")')
        page.wait_for_timeout(600)
        got = total_shown()
        check(got == want["Personal"], "Personal-only returns exactly the Personal posts",
              f"{got:,} shown vs {want['Personal']:,} in the index")
        check("src=" in page.url, "source filter is in the URL", page.url[-40:])

        # An explicit source must beat the newsroom toggle, or asking for
        # Newsroom while newsrooms are hidden silently returns nothing.
        page.click('#sources .chip:has-text("Personal")')   # off
        page.click('#sources .chip:has-text("Newsroom")')
        page.wait_for_timeout(600)
        got = total_shown()
        check(got == want["Newsroom"],
              "Newsroom-only returns exactly the Newsroom posts, despite the hide toggle",
              f"{got:,} shown vs {want['Newsroom']:,} in the index")
        check(page.evaluate("() => document.querySelector('#hide-news').disabled"),
              "the newsroom toggle is disabled while a source is chosen")
        page.click("#reset")
        page.wait_for_timeout(600)
        check(not page.evaluate("() => document.querySelector('#hide-news').disabled"),
              "clearing filters re-enables the newsroom toggle")

        # --- date range ---
        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        import datetime as _dt
        want = f"Since {_dt.datetime.utcnow().year - 8}"
        got = page.locator('[data-since="8"]').inner_text().strip()
        check(got == want, "the relative date label names the right year",
              f"{got!r} vs {want!r}")

        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        all_total = page.evaluate("() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")
        page.click('[data-since="1"]')
        page.wait_for_timeout(500)
        yr_total = page.evaluate("() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")
        check(0 < yr_total < all_total, "Past year narrows the corpus",
              f"{all_total:,} -> {yr_total:,}")
        import datetime
        cutoff = datetime.datetime.utcnow().year - 1
        years = page.evaluate(
            "() => [...document.querySelectorAll('#results .r-year')].map(e => +e.textContent)")
        check(years and min(years) >= cutoff, "Past year rows are actually recent",
              f"oldest shown {min(years) if years else 'n/a'}")

        # --- hide dead links ---
        page.goto(base + "?sort=oldest", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        before = page.locator(".r-dead").count()
        page.check("#hide-dead")
        page.wait_for_timeout(500)
        after = page.locator(".r-dead").count()
        check(before > 0 and after == 0, "hiding dead links removes every flagged row",
              f"{before} -> {after}")
        check("dead=0" in page.url, "dead-link filter survives in the URL", page.url)

        # --- subscribe ---
        page.goto(base + "?mode=blogs", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        n_rss = page.locator("#results a.feed-link").count()
        check(n_rss > 0, "blog rows offer a subscribe link", f"{n_rss} of 40 rows")
        href = page.locator("#results a.feed-link").first.get_attribute("href")
        check((href or "").startswith("http"), "subscribe link is an absolute feed URL", href or "")
        check(not page.locator("#opml").is_hidden(), "OPML export offered in blogs mode")

        # Dormancy is shown only where it is news. A "last active" stamp on a
        # blog that posted this month is noise; on one that stopped in 2016 it
        # is the most useful thing on the row.
        import datetime as _d
        cy = _d.datetime.utcnow().year
        page.goto(base + "?mode=blogs&sort=oldest", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(400)
        years = page.evaluate(
            "() => [...document.querySelectorAll('.r-dormant')].map(e => +e.textContent.match(/\\d{4}/)[0])")
        check(len(years) > 0, "dormant blogs are labelled", f"{len(years)} of 40 rows")
        check(all(y < cy - 1 for y in years),
              "only genuinely dormant blogs are labelled",
              f"newest labelled {max(years) if years else 'n/a'}, cutoff {cy - 2}")
        page.goto(base + "?mode=blogs&since=1", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(400)
        check(page.locator(".r-dormant").count() == 0,
              "filtering to active blogs leaves no dormant labels",
              f"{page.locator('.r-dormant').count()} labelled")
        page.goto(base + "?mode=blogs", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)

        # The action group must not overlap: two absolutely-positioned links
        # would, because "5 posts" and "1,284 posts" are different widths.
        overlap = page.evaluate("""() => {
            let bad = 0;
            for (const g of document.querySelectorAll('.row-actions')) {
                const ks = [...g.children].map(c => c.getBoundingClientRect());
                for (let i = 1; i < ks.length; i++)
                    if (ks[i].left < ks[i-1].right - 0.5) bad++;
            }
            return bad;
        }""")
        check(overlap == 0, "row actions never overlap", f"{overlap} overlapping pairs")

        with page.expect_download(timeout=20000) as dl:
            page.click("#opml")
        path = dl.value.path()
        opml = Path(path).read_text(encoding="utf-8")
        n_out = opml.count("<outline ")
        check(opml.startswith("<?xml"), "OPML export downloads a real XML file")
        check(n_out > 50, "OPML carries the whole filtered blogroll, not the page",
              f"{n_out} feeds (page showed 40)")
        check('xmlUrl="http' in opml, "OPML outlines carry xmlUrl")
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(opml)
            check(True, "OPML parses as well-formed XML")
        except Exception as exc:
            check(False, "OPML parses as well-formed XML", str(exc)[:60])

        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        check(page.locator("#opml").is_hidden(), "OPML export hidden in posts mode")

        # --- progressive load ---
        #
        # titles.txt streams in score order and search goes live on the first
        # chunk, so a query typed mid-load runs against a partial corpus. The
        # danger is not the partial answer -- it is the cached match set: uFuzzy
        # narrows a new query from the previous result, and a set computed
        # before the rest of the corpus arrived would keep hiding those rows for
        # the whole session, with a perfectly plausible count. Unthrottled this
        # window is milliseconds wide, so force it open.
        page.goto("about:blank")
        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 40,
            "downloadThroughput": 3_000_000 / 8, "uploadThroughput": 3_000_000 / 8})
        page.goto(base, wait_until="commit")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=120000)
        page.fill("#q", "rust")
        page.wait_for_timeout(150)
        page.fill("#q", "rust async")     # extend the query -> narrowing path
        page.wait_for_timeout(150)
        mid = page.evaluate(
            "() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")
        # `or mid >= 0` made this unconditionally true -- .hl only ever holds a
        # non-negative count -- so the real assertion was never evaluated. At
        # 3 Mbps the corpus takes many seconds, so the note MUST have appeared.
        try:
            page.wait_for_selector("#load-note:not([hidden])", timeout=20000)
            seen_note = True
        except Exception:
            seen_note = False
        check(seen_note, "load progress is disclosed while the corpus streams",
              "#load-note appeared" if seen_note
              else "#load-note never became visible under 3 Mbps throttling")
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0,
            "downloadThroughput": -1, "uploadThroughput": -1})
        page.wait_for_function("() => document.querySelector('#load-note').hidden",
                               timeout=120000)
        page.wait_for_timeout(600)
        final = page.evaluate(
            "() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")

        # Same query from a cold page, whole corpus present.
        page.goto(base + "?q=rust+async", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=120000)
        page.wait_for_function("() => document.querySelector('#load-note').hidden",
                               timeout=120000)
        page.wait_for_timeout(600)
        cold = page.evaluate(
            "() => +document.querySelector('.status .hl').textContent.replace(/,/g,'')")
        check(final == cold,
              "a query typed mid-load ends up with the full result set",
              f"streamed {final} vs cold {cold}")
        check(mid <= final, "mid-load count only grows", f"{mid} -> {final}")

        # --- similar blogs ---
        page.goto(base + "?b=jvns.ca", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector(".pin-similar .sim-chip", timeout=20000)
        chips = page.locator(".pin-similar .sim-chip")
        names = [chips.nth(i).inner_text() for i in range(chips.count())]
        check(len(names) >= 3, "pinned blog gets recommendations", ", ".join(names[:3]))
        check("wizardzines.com" in names,
              "recommendations find the obvious neighbour", ", ".join(names))
        check("jvns.ca" not in names, "a blog is never similar to itself")

        # One shared rare token is a person's name, not a subject. These two
        # matched jvns.ca purely on the surname "Evans" before the two-term rule.
        check(not ({"ben-evans.com", "domainlanguage.com"} & set(names)),
              "surname collisions are not recommendations", ", ".join(names))

        page.click(".pin-similar .sim-chip")
        page.wait_for_timeout(500)
        pinned = page.locator(".pin-name").inner_text()
        check(pinned == names[0], "clicking a recommendation pins it",
              f"{pinned} vs {names[0]}")
        rows_blogs = page.evaluate(
            "() => [...new Set([...document.querySelectorAll('#results .r-blog')].map(e => e.textContent))]")
        check(rows_blogs == [pinned], "the newly pinned blog owns the results",
              str(rows_blogs)[:60])

        # Recommendations honour the newsroom toggle. therecord.media is trade
        # press (visible) and used to be recommended trendmicro.com, a vendor
        # blog the reader had explicitly hidden.
        page.goto(base + "?b=therecord.media", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector(".pin-similar", timeout=20000)
        page.wait_for_timeout(500)
        rec = page.evaluate(
            "() => [...document.querySelectorAll('.sim-chip')].map(e => e.textContent)")
        check("trendmicro.com" not in rec,
              "hidden sources are not recommended while hidden", ", ".join(rec)[:70])
        page.uncheck("#hide-news")
        page.wait_for_timeout(900)
        rec2 = page.evaluate(
            "() => [...document.querySelectorAll('.sim-chip')].map(e => e.textContent)")
        check(rec2 != rec, "unhiding newsrooms refreshes the recommendations",
              ", ".join(rec2)[:70])

        # A reader already looking at a hidden source reached it deliberately;
        # answering "what else is like The Spectator" with nothing would be
        # worse than answering with other magazines.
        page.goto(base + "?b=spectator.co.uk", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector(".pin-similar", timeout=20000)
        page.wait_for_timeout(500)
        n_self = page.locator(".sim-chip").count()
        check(n_self > 0, "a pinned hidden source still gets recommendations",
              f"{n_self} chips")

        # Same publisher's other properties are not a recommendation.
        page.goto(base + "?b=blog.cloudflare.com", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector(".pin-similar", timeout=20000)
        page.wait_for_timeout(400)
        cf = page.evaluate(
            "() => [...document.querySelectorAll('.pin-similar .sim-chip')].map(e => e.textContent)")
        n_same = sum(1 for c in cf if c.endswith("cloudflare.com"))
        check(n_same <= 1, "at most one sibling property is recommended",
              f"{n_same} of {len(cf)}: {', '.join(cf)}")

        # --- streaming must not steal the keyboard cursor or flood the live region ---
        #
        # Search goes live on the first chunk, so the reader is arrowing through
        # results while the corpus is still arriving. Each streaming tick
        # re-runs the query, and render() empties the list and calls
        # selectRow(-1) -- which moves real DOM focus. Three times a second.
        page.goto("about:blank")
        cdp2 = page.context.new_cdp_session(page)
        cdp2.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 40,
            "downloadThroughput": 3_000_000 / 8, "uploadThroughput": 3_000_000 / 8})
        page.goto(base, wait_until="commit")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=120000)
        page.wait_for_selector("#results > li", timeout=60000)
        live_while_loading = page.evaluate(
            "() => document.querySelector('#status').getAttribute('aria-live')")
        check(live_while_loading == "off",
              "the live region is silenced while the corpus streams",
              f"aria-live={live_while_loading!r}")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        before = page.evaluate(
            "() => [...document.querySelectorAll('#results > li')].findIndex(l => l.classList.contains('sel'))")
        page.wait_for_timeout(1800)      # several streaming ticks
        after = page.evaluate("""() => ({
            sel: [...document.querySelectorAll('#results > li')].findIndex(l => l.classList.contains('sel')),
            focusInResults: document.querySelector('#results').contains(document.activeElement),
        })""")
        check(before >= 0 and after["sel"] >= 0,
              "the keyboard cursor survives streaming re-runs",
              f"row {before} -> row {after['sel']}")
        check(after["focusInResults"],
              "focus stays inside the results while streaming",
              f"activeElement in #results: {after['focusInResults']}")
        cdp2.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0,
            "downloadThroughput": -1, "uploadThroughput": -1})
        page.wait_for_function("() => document.querySelector('#load-note').hidden", timeout=120000)
        page.wait_for_timeout(700)
        live_after = page.evaluate(
            "() => document.querySelector('#status').getAttribute('aria-live')")
        check(live_after == "polite", "the live region goes polite once loaded",
              f"aria-live={live_after!r}")

        # --- segmented controls expose their selection ---
        page.goto(base + "?sort=points&since=3", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(300)
        seg = page.evaluate("""() => {
            const grab = (sel) => [...document.querySelectorAll(sel)].map(b => [
                b.textContent.trim(), b.getAttribute('aria-pressed'), b.classList.contains('active')]);
            return {sort: grab('[data-sort]'), since: grab('[data-since]')};
        }""")
        for name, rows in seg.items():
            marked = [r for r in rows if r[1] == "true"]
            agrees = all((r[1] == "true") == r[2] for r in rows)
            check(len(marked) == 1 and agrees,
                  f"the {name} control announces exactly one selection",
                  f"{[r[0] for r in marked]} pressed, class/aria agree: {agrees}")

        # --- mobile ---
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
        check(not overflow, "no horizontal overflow on mobile")

        # How much chrome sits above the first result on a phone?
        page.set_viewport_size({"width": 360, "height": 640})
        # Navigate explicitly. This used to reload whatever the previous block
        # left in the address bar, so the headroom it measured depended on test
        # order -- it silently started measuring a pinned blog.
        page.goto(base, wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        top = page.evaluate(
            "() => document.querySelector('#results > li').getBoundingClientRect().top")
        visible = page.evaluate(
            "() => [...document.querySelectorAll('#results > li')].filter(e => e.getBoundingClientRect().top < 640).length")
        check(top < 480, "first result is reachable on a 360x640 phone", f"y={top:.0f}px")
        check(visible >= 1, "at least one result in the opening viewport", f"{visible} rows")
        # An absolutely-positioned action pill has empty gutter to float into
        # only while the row is wide. On a phone it landed on top of the topic
        # tags -- and on touch, where the hover reveal never fires, it stayed
        # there. Assert no action link overlaps row content at phone width.
        # 600px as well as 360px: just above the 560px breakpoint is where the
        # floating layout resumes with the least gutter to float into, which is
        # exactly where it would start colliding again.
        for width, url in ((360, "?mode=blogs"), (360, "?sort=oldest"),
                           (600, "?mode=blogs"), (600, "?sort=oldest"),
                           (768, "?mode=blogs"), (860, "?sort=oldest"),
                           (900, "?mode=blogs"), (1280, "?sort=oldest")):
            page.set_viewport_size({"width": width, "height": 800})
            page.goto(base + url, wait_until="load")
            page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
            page.wait_for_selector("#results > li", timeout=20000)
            page.wait_for_timeout(300)
            clash = page.evaluate("""() => {
                const hit = (a, b) => a.left < b.right && b.left < a.right &&
                                      a.top < b.bottom && b.top < a.bottom;
                let bad = [];
                for (const li of document.querySelectorAll('#results > li')) {
                    const acts = [...li.querySelectorAll('.hn-link')];
                    const content = [...li.querySelectorAll('.r-title, .r-desc, .r-meta > *')];
                    for (const a of acts)
                        for (const c of content)
                            if (hit(a.getBoundingClientRect(), c.getBoundingClientRect()))
                                bad.push(a.textContent.trim() + ' over ' + c.textContent.trim());
                }
                return bad;
            }""")
            check(not clash, f"actions clear row content at {width}px ({url})",
                  (clash[0] if clash else "none")[:60])

        page.screenshot(path=os.path.join(SHOTS, "mobile.png"))

        # A pinned blog adds a header, a description and a recommendation row
        # above the results. That is the deepest the chrome ever gets, so it is
        # the case worth asserting, not the shallowest.
        page.set_viewport_size({"width": 360, "height": 640})
        page.goto(base + "?b=jvns.ca", wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        page.wait_for_timeout(400)
        ptop = page.evaluate(
            "() => document.querySelector('#results > li').getBoundingClientRect().top")
        check(ptop < 500, "first result stays reachable with a blog pinned on a phone",
              f"y={ptop:.0f}px")

        browser.close()
    httpd.shutdown()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("all browser checks passed")


if __name__ == "__main__":
    main()
