#!/usr/bin/env python3
"""Distil a link-check crawl into pipeline/dead_urls.txt.

A "link may be dead" badge is a claim about the article, so only evidence
about the article counts.

- 404, 410 and 451 are the server saying the page is gone. Dead. (Re-probed:
  32 of 32 sampled load neither on HEAD nor on GET.)
- A connection failure -- refused, timed out, broken TLS -- is evidence about
  the HOST, seen from ONE machine: the one that just sent that host hundreds of
  requests. rachelbythebay.com refused all 213 because it banned the crawler;
  npr.org refused 535 while its home page answers 200. So a refusal becomes a
  dead link only when the host is gone for everyone. Certain, never overridden:
    * public DNS -- two DNS-over-HTTPS providers that must agree, never this
      machine's resolver -- says the name does not exist or has no address; or
      it is not a loadable public host. This machine's resolver is a home
      router, and it answered NXDOMAIN for blog.openai.com and a dozen other
      names that resolve everywhere else.
  Otherwise it is dead only if NONE of these holds:
    * it served any page during the crawl;
    * it published a post in the last year -- you cannot publish on a dead host;
    * its home page answers now, or it accepts a connection and then stalls;
    * the Wayback Machine -- a vantage point this crawl cannot have poisoned --
      reached its home page within the last year. jwz.org and mondaynote.com
      both refuse this machine; archive.org fetched both in the last year.
  Evidence that cannot be gathered flags nothing: a missing warning costs a
  reader one click, a false one misleads them.

Only URLs the index builds are kept, so results for URLs it no longer produces
(the doubled medium.com/@a/@a/... form) cannot leave stale entries behind.

    distill_dead_urls.py work/linkcheck.jsonl blogs/data pipeline/dead_urls.txt [work/dead_hosts.json]
"""
import datetime, ipaddress, json, os, socket, struct, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_index import dead_key, post_url  # noqa: E402  -- the one join rule

GONE = {404, 410, 451}
RECENT = datetime.timedelta(days=365)
DAY0 = datetime.date(2006, 1, 1)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BOT_UA = "erenozen.dev-linkcheck/0.1 (+https://erenozen.dev/blogs/)"
NXDOMAIN = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
RESERVED = (".local", ".localhost", ".test", ".example", ".invalid", ".internal",
            ".lan", ".home.arpa")
CERTAIN = {"name does not exist (public DNS)", "name has no address (public DNS)",
           "not a valid hostname", "not a public host"}
DOH = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")


def hostname(netloc):
    try:
        return urlparse("//" + netloc).hostname or ""
    except ValueError:
        return ""


def public_host(name):
    h = name.lower().rstrip(".")
    if not h or h == "localhost" or h.endswith(RESERVED):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_unspecified
                or ip.is_link_local or ip.is_reserved)


def load_index(d):
    """Every article URL the index links to, and the newest post date per host."""
    meta = json.load(open(os.path.join(d, "meta.json")))
    n = meta["n_posts"]
    blogs = json.load(open(os.path.join(d, "blogs.json")))
    with open(os.path.join(d, "paths.txt"), encoding="utf-8") as f:
        paths = f.read().split("\n")
    with open(os.path.join(d, "posts.bin"), "rb") as f:
        buf = f.read()
    bid = struct.unpack_from(f"<{n}I", buf, 0)
    day = struct.unpack_from(f"<{n}H", buf, n * 6)
    urls, newest = {}, {}
    for i in range(n):
        u = post_url(blogs[bid[i]]["h"], paths[i])
        urls[dead_key(u)] = u
        h = urlparse(u).netloc.lower()
        if day[i] > newest.get(h, -1):
            newest[h] = day[i]
    return urls, {h: DAY0 + datetime.timedelta(days=v) for h, v in newest.items()}


def dns_state(name):
    """'ok', 'nx' (the name does not exist), 'invalid' or 'unclear'."""
    for attempt in range(2):
        try:
            socket.getaddrinfo(name, 443)
            return "ok"
        except (UnicodeError, ValueError):
            return "invalid"
        except socket.gaierror as e:
            if e.errno in NXDOMAIN:
                return "nx"
            time.sleep(2)
    return "unclear"


def doh_state(name, session):
    """Ask public DNS over HTTPS: 'resolves', 'nx' (the name does not exist),
    'nodata' (it exists with no address) or 'unclear'.

    Both providers must agree before a name is called gone, and one provider
    that resolves it is enough to keep checking. A SERVFAIL or an unreachable
    provider is 'unclear', which is never taken as proof of anything."""
    votes = []
    for url in DOH:
        verdict = "unclear"
        for rtype in ("A", "AAAA"):
            try:
                r = session.get(url, params={"name": name, "type": rtype},
                                headers={"accept": "application/dns-json"}, timeout=10)
                j = r.json()
            except (requests.RequestException, ValueError):
                break
            if any(a.get("type") in (1, 28) for a in (j.get("Answer") or [])):
                verdict = "resolves"
                break
            st = j.get("Status")
            if st == 3:
                verdict = "nx"
                break
            if st == 0:
                verdict = "nodata"               # holds only if AAAA is empty too
                continue
            verdict = "unclear"
            break
        votes.append(verdict)
    if "resolves" in votes:
        return "resolves"
    if votes and all(v == "nx" for v in votes):
        return "nx"
    if votes and all(v in ("nx", "nodata") for v in votes):
        return "nodata"
    return "unclear"


def home_state(host, session):
    """("up" | "gone", why) from one request to the home page."""
    tls_broken = False
    for scheme in ("https", "http"):
        try:
            r = session.get(f"{scheme}://{host}/", timeout=10,
                            allow_redirects=True, stream=True)
            r.close()
            return "up", "home page answers now"
        except requests.exceptions.SSLError:
            if scheme == "http":
                # Port 80 answered -- with a redirect to https, whose certificate
                # is broken. The host is up; a reader sees a certificate warning,
                # not a dead link. Twelve of thirteen "TLS broken" hosts were this
                # (jeffknupp.com, semiaccurate.com), logged as refusals that never
                # happened.
                return "up", "answers HTTP; its TLS certificate is broken"
            tls_broken = True                      # something listens on 443
            continue
        except requests.exceptions.ReadTimeout:
            return "up", "accepts connections, then stalls"
        except requests.exceptions.ConnectTimeout:
            return "gone", "nothing accepts a connection here"
        except requests.exceptions.ConnectionError as e:
            msg = str(e).lower()
            # A name that fails to resolve HERE surfaces as a ConnectionError too,
            # and used to fall through to "accepts connections, then drops them"
            # -- a false reason, and it skipped the outside vantage point.
            if ("failed to resolve" in msg or "name or service not known" in msg
                    or "no address associated" in msg or "nameresolution" in msg):
                return "gone", "does not resolve from this machine"
            if "refused" in msg or "unreachable" in msg or "newconnectionerror" in msg:
                return "gone", ("TLS broken and HTTP refused here" if tls_broken
                                else "connection refused here")
            return "up", "accepts connections, then drops them"
        except requests.RequestException:
            return "up", "home page answers now"
    return "gone", "TLS broken and HTTP refused here"


def _json(session, url, params, timeout):
    """GET JSON with backoff on 429/5xx. None if it cannot be had."""
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        try:
            return r.json() if r.text.strip() else []
        except ValueError:
            return None
    return None


def wayback(host, session, since):
    """(True, date) if archive.org reached the home page on or after `since`,
    (False, date-or-"never") if not, (None, why) if it could not be asked."""
    stamp = since.strftime("%Y%m%d")
    data = _json(session, "https://archive.org/wayback/available",
                 {"url": host + "/", "timestamp": time.strftime("%Y%m%d")}, 20)
    snap = ((data or {}).get("archived_snapshots") or {}).get("closest") if isinstance(data, dict) else None
    if snap:
        ts, st = snap.get("timestamp", ""), str(snap.get("status", ""))
        if ts >= stamp and st[:1] in ("2", "3"):
            return True, ts[:8]
    # The availability API sometimes answers empty for hosts with plenty of
    # captures (stephendiehl.com), and only reports the single closest one. Ask
    # the capture index for any good capture in the window, bounded by date.
    rows = _json(session, "https://web.archive.org/cdx/search/cdx",
                 {"url": host + "/", "from": stamp, "limit": "5", "output": "json",
                  "fl": "timestamp,statuscode"}, 25)
    if rows is None:
        return (False, snap.get("timestamp", "")[:8]) if snap else (None, "unreachable")
    good = [ts for ts, st in rows[1:] if st[:1] in ("2", "3")]
    if good:
        return True, good[0][:8]
    return False, (snap.get("timestamp", "")[:8] if snap else "never")


def main():
    crawl, index_dir, out = sys.argv[1:4]
    hosts_out = sys.argv[4] if len(sys.argv) > 4 else None
    workers = int(os.environ.get("LC_HOST_WORKERS", "16"))
    since = datetime.date.today() - RECENT

    # Crawl results are matched to the index by dead_key(), not by exact URL:
    # the crawl probed whichever host form the index built at the time, and
    # blog homes used to drop "www.". Every verdict below is about the URL the
    # index links to NOW -- www.stephendiehl.com, which resolves, rather than
    # the bare stephendiehl.com the crawl was sent to, which has no address.
    status, served = {}, set()
    for line in open(crawl):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        status[dead_key(r["url"])] = r["status"]  # the latest result wins
        if 0 < r["status"] < 400:
            served.add(urlparse(r["url"]).netloc.lower().removeprefix("www."))

    wanted, newest = load_index(index_dir)       # dead_key -> the URL the index links to
    seen = {k: s for k, s in status.items() if k in wanted}
    dead = {wanted[k] for k, s in seen.items() if s in GONE}
    n_gone = len(dead)
    refused = defaultdict(list)
    for k, s in seen.items():
        if s < 0:
            u = wanted[k]
            refused[urlparse(u).netloc.lower()].append(u)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html,*/*"})

    doh = requests.Session()
    doh.headers["User-Agent"] = BOT_UA

    def first(h):
        name = hostname(h)
        try:
            if not public_host(name):
                return h, ("gone", "not a public host", "")
            try:
                name.encode("idna")
            except UnicodeError:
                return h, ("gone", "not a valid hostname", "")
            if h.removeprefix("www.") in served:
                return h, ("up", "served pages during the crawl", "")
            pub = doh_state(name, doh)
            if pub == "nx":
                return h, ("gone", "name does not exist (public DNS)", "")
            if pub == "nodata":
                return h, ("gone", "name has no address (public DNS)", "")
            if newest.get(h, DAY0) >= since:
                return h, ("up", "published within the last year", str(newest[h]))
            # Neither of these is proof; the Wayback Machine decides them.
            if pub == "unclear":
                return h, ("gone", "public DNS fails for it", "")
            if dns_state(name) != "ok":
                return h, ("gone", "does not resolve from this machine", "")
            st, why = home_state(h, session)
            return h, (st, why, "")
        except Exception as e:  # noqa: BLE001 -- one host must never abort the run
            return h, ("up", "probe error", type(e).__name__)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        verdict = dict(ex.map(first, sorted(refused)))

    ask = sorted(h for h, v in verdict.items() if v[0] == "gone" and v[1] not in CERTAIN)
    wb = requests.Session()
    wb.headers["User-Agent"] = BOT_UA

    def second(h):
        time.sleep(0.5)                            # archive.org is a shared resource
        ok, info = wayback(h, wb, since)
        if ok:
            return h, ("up", "the Wayback Machine reached it this year", info)
        if ok is None:
            return h, ("up", "Wayback Machine could not be asked", info)
        return h, ("gone", verdict[h][1] + "; not archived since " + str(since), f"last: {info}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        verdict.update(ex.map(second, ask))

    why, rescued, kept = Counter(), Counter(), Counter()
    for h, urls in refused.items():
        st, reason, _ = verdict[h]
        why[f"{st}: {reason}"] += len(urls)
        if st == "gone":
            dead.update(urls)
            kept[h] += len(urls)
        else:
            rescued[h] += len(urls)

    with open(out, "w") as f:
        f.write("# URLs the index links to that are gone (see pipeline/distill_dead_urls.py).\n")
        f.write(f"# {len(seen):,} of {len(wanted):,} indexed URLs checked; {len(dead):,} gone: "
                f"{n_gone:,} answered 404/410/451, {len(dead) - n_gone:,} are on hosts that no "
                f"longer resolve or are unreachable here and from the Wayback Machine. "
                f"Distilled {datetime.date.today()}.\n")
        for u in sorted(dead):
            f.write(u + "\n")
    if hosts_out:
        with open(hosts_out, "w") as f:
            json.dump({h: {"state": v[0], "why": v[1], "detail": v[2], "urls": len(refused[h])}
                       for h, v in sorted(verdict.items())}, f, indent=1)

    n_refused = sum(len(v) for v in refused.values())
    print(f"indexed URLs: {len(wanted):,}; checked: {len(seen):,}; not yet checked: {len(wanted) - len(seen):,}")
    print(f"404/410/451: {n_gone:,}  ->  dead")
    print(f"connection failures: {n_refused:,} on {len(refused):,} hosts; "
          f"{len(ask)} needed the Wayback Machine")
    for k, v in why.most_common():
        print(f"    {v:6,d}  {k}")
    print(f"dead URLs written: {len(dead):,}  (was {n_gone + n_refused:,} under the first rule; "
          f"{sum(rescued.values()):,} rescued)")
    print("largest rescues: " + ", ".join(f"{h}={n}" for h, n in rescued.most_common(8)))
    print("largest still dead by connection: " + ", ".join(f"{h}={n}" for h, n in kept.most_common(8)))


if __name__ == "__main__":
    main()
