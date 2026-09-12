#!/usr/bin/env python3
"""HEAD-check indexed post URLs to find dead links.

Politeness matters more than speed here: 111k URLs across ~6k hosts. Work is
bucketed BY HOST and each host is processed by a single worker sequentially with
a delay between requests, so we never open parallel connections to the same
site. Hosts run concurrently; a host never runs against itself.

Oldest posts first -- link rot correlates with age, so partial results still
cover the riskiest part of the corpus.

Resumable: appends to JSONL and skips anything already recorded.
"""
import json, os, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

UA = "Mozilla/5.0 (compatible; erenozen.dev-linkcheck/0.1; +https://erenozen.dev/blogs/)"
TIMEOUT = int(os.environ.get("LC_TIMEOUT", "8"))
PER_HOST_DELAY = float(os.environ.get("LC_DELAY", "0.25"))


def load_index(d):
    import struct
    meta = json.load(open(os.path.join(d, "meta.json")))
    blogs = json.load(open(os.path.join(d, "blogs.json")))
    with open(os.path.join(d, "paths.txt"), encoding="utf-8") as f:
        paths = f.read().split("\n")
    n = meta["n_posts"]
    with open(os.path.join(d, "posts.bin"), "rb") as f:
        buf = f.read()
    blog_ids = struct.unpack_from(f"<{n}I", buf, 0)
    day = struct.unpack_from(f"<{n}H", buf, n * 6)
    out = []
    for i in range(n):
        b = blogs[blog_ids[i]]
        # A path is relative to the blog home, or a full URL for a post on
        # another host -- the same rule as build_index.post_url, which is what
        # dead_urls.txt is joined against.
        p = paths[i]
        url = p if p.startswith(("http://", "https://")) else b["h"].rstrip("/") + p
        out.append((i, url, day[i]))
    return out


def _probe(url, session):
    """One request: HEAD, falling back to a ranged GET for servers that
    mishandle HEAD. Returns (status, final_url); negative status on failure."""
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (403, 405, 501) or r.status_code >= 500:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True,
                            stream=True, headers={"Range": "bytes=0-2048"})
            r.close()
        return r.status_code, r.url
    except requests.exceptions.SSLError:
        return -2, ""
    except requests.exceptions.ConnectionError:
        return -3, ""
    except requests.exceptions.Timeout:
        return -4, ""
    except Exception:
        return -1, ""


def check_host(host, items, session):
    """Sequentially check every URL for one host."""
    res = []
    for idx, url in items:
        status, final = _probe(url, session)

        # Blog keys are all built as https://, but plenty of 2007-era blogs only
        # ever served plain HTTP. Without this retry an absent or expired
        # certificate reads as "dead" -- measured at 12.6% of the corpus, and it
        # wrongly condemned esr.ibiblio.org and aaronsw.com. A reader following
        # the link reaches the page, so the checker should too.
        if status in (-2, -3) and url.startswith("https://"):
            time.sleep(PER_HOST_DELAY)
            alt, alt_final = _probe("http://" + url[len("https://"):], session)
            if alt > 0:
                status, final = alt, alt_final

        res.append({"i": idx, "url": url, "status": status,
                    "final": final if final != url else ""})
        time.sleep(PER_HOST_DELAY)
    return res


def main():
    d, out_path = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 32
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    done = set()
    if os.path.exists(out_path):
        for line in open(out_path):
            try:
                done.add(json.loads(line)["url"])
            except Exception:
                pass
        print(f"resuming: {len(done)} already checked", flush=True)

    posts = [p for p in load_index(d) if p[1] not in done]
    posts.sort(key=lambda p: p[2])          # oldest first
    if limit:
        posts = posts[:limit]

    by_host = defaultdict(list)
    seen_url = set()
    for idx, url, _ in posts:
        if url in seen_url:
            continue
        seen_url.add(url)
        by_host[urlparse(url).netloc.lower()].append((idx, url))
    print(f"{len(posts)} urls across {len(by_host)} hosts", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "*/*"})

    n_done = n_dead = 0
    out = open(out_path, "a")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_host, h, items, session): h
                for h, items in by_host.items()}
        for fut in as_completed(futs):
            try:
                rows = fut.result()
            except Exception:
                continue
            for r in rows:
                out.write(json.dumps(r) + "\n")
                n_done += 1
                if r["status"] <= 0 or r["status"] >= 400:
                    n_dead += 1
            out.flush()
            if n_done % 2000 < len(rows):
                print(f"  {n_done}/{len(posts)} checked, {n_dead} dead "
                      f"({100*n_dead/max(n_done,1):.1f}%)", flush=True)
    out.close()
    print(f"DONE: {n_done} checked, {n_dead} dead ({100*n_dead/max(n_done,1):.1f}%)")


if __name__ == "__main__":
    main()
