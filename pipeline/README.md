# Blog finder pipeline

Builds the static search index served at `/blogs/`. Everything is derived from
two sources: Hacker News (via the Algolia API) and the blogs' own RSS/Atom feeds.

## Why it is shaped this way

HN ranks **posts**, but we want **blogs**, so stage 2 groups stories by publisher
and ranks by *number of distinct stories that cleared the bar* — never by total
points, which one viral post would dominate.

The head of that ranking is almost entirely news sites (`arstechnica`, `nytimes`,
`theguardian`), while the blogs people actually want sit in the tail. No scalar
separates them: `netflixtechblog.com` is corporate *and* excellent, `lwn.net` is
structurally a newsroom *and* essential. So `source` is a discrete label assigned
by an LLM, and the UI hides `newsroom`/`vendor`/`institution` behind one toggle.

## Stages

| # | script | in → out |
|---|---|---|
| 1 | `pull_hn.py` | Algolia → `hn_stories.jsonl` (507k stories ≥25 pts, 2006–now) |
| 2 | `dedupe.py` | → `hn_dedup.jsonl` (463k; collapses ~4.5% duplicate submissions) |
| 3 | `aggregate_domains.py` | → `candidates.jsonl` (18.3k candidate blogs, ≥3 stories) |
| 4 | `select_for_feeds.py` + `fetch_feeds.py` | → `feeds.jsonl` (63% feed discovery rate) |
| 5 | `build_evidence.py` | → `evidence/ev_NNN.txt` LLM batch files |
| 6 | *(out of band)* | LLM classification → `classified/cls_NNN.jsonl` |
| 6b | `validate_classifications.py` | fails if a batch was skipped or duplicated |
| 7 | `build_index.py` | → `blogs/data/*` |
| 8 | `check_index.py` | fails CI if the index is inconsistent |
| 9 | `sync_counts.py` | rewrites the corpus size quoted in prose on two pages |
| 10 | `update_feed_urls.py` | merges this run's discoveries back into `feed_urls.tsv` |

`select_for_feeds.py` limits stage 4 to blogs that survived classification, so we
never crawl candidates that were never going to be indexed.

`check_links.py` is stage 0 of nothing — it runs by hand, HEAD-checks every
indexed URL (154k, ~10h) and writes raw results to `work/linkcheck.jsonl`;
`distill_dead_urls.py` turns those into `dead_urls.txt`, which `build_index.py`
takes as an optional last argument.

The distiller only trusts evidence about the article. A 404/410/451 is dead. A
connection failure is evidence about the *host*, seen from one machine -- the one
that just sent it hundreds of requests: rachelbythebay.com banned the crawler and
refused all 213. So a refusal becomes a dead link only when the host is gone for
everyone: public DNS -- two DNS-over-HTTPS providers that must agree, never this
machine's resolver, which is a home router that called blog.openai.com
nonexistent -- says the name does not exist or has no address (certain); or it
refuses here AND it served
no page during the crawl, published nothing in the last year, does not answer its
home page now, and the Wayback Machine -- a vantage point the crawl cannot poison
-- has not reached it within the year. Evidence that cannot be gathered flags
nothing. Every host's verdict and reason is written to `work/dead_hosts.json`. 12% of the corpus no longer resolves, decaying
from 32% of 2009 posts to 4% of 2025. Far too slow for CI, so the distilled list
is committed and replayed; link rot only goes one way, so replaying is accurate
between crawls.

`feed_urls.tsv` is what makes stage 4 affordable. It records, per blog, either
the resolved feed URL or `-` for "discovery found nothing". The negatives are
the point: a blog whose HTML advertises a feed resolves in one request, while a
blog with no feed costs 14 that all time out -- and 4,350 of 10,622 known blogs
have no feed. Measured, the same 200 feedless blogs went from *not finishing in
ten minutes* to 0.16 seconds. Re-deriving that answer monthly is what made a
full pass take four hours. Regenerate with `SKIP_NEGATIVE=0` to pick up blogs
that have since added a feed.

`search_eval.py` measures recall against ground truth the search did not
produce: for a query, every title literally containing all its terms, versus
what the worker actually returns. A miss only counts when the page was not
full. It caught "writing a compiler" returning 34 rows of a 40-row page while
38 titles matched.

`browser_test.py` drives the built site in headless Chrome. Everything else here
reasons about code that was never rendered; this is what catches an action pill
sitting on top of a topic tag, or a result count that silently reports the page
size instead of the match count.

## Index layout

`blogs/data/` is six files that must agree row-for-row. Nothing inside the index
can detect a mismatch between them: every file stays individually well-formed and
the UI happily renders one post's title beside another's URL.

| file | contents | when it loads |
|---|---|---|
| `meta.json` | taxonomy, counts, hidden-source mask | blocking |
| `blogs.json` | per-blog name, home, feed, topics, quality | blocking |
| `posts.bin` | 12 B/post: blogId u32, points u16, day u16, topicMask u16, kindSource u8, score u8 | blocking |
| `titles.txt` | one title per line | streamed; search goes live on chunk 1 |
| `paths.txt` | per post: a path relative to the blog home, or a full URL for a post on another host | deferred |
| `hn.bin` | 4 B/post: HN item id | deferred, after `paths.txt` |

Every column is ordered by **descending score**, which is what makes streaming
titles useful: the first bytes off the wire are the best posts, not arbitrary
ones. `check_index.py` holds eight (HN id → title) pairs verified against the live
Algolia API, because a permutation bug here is otherwise invisible.

A path is joined to its blog's home by `post_url()` in `build_index.py` -- the
renderer and `check_links.py` apply the same rule, and `dead_urls.txt` is keyed
on its output. Two things make that non-obvious. Path-platform blogs keep the
author in the home (`medium.com/@bellmar`), so the stored path is relative to
it, not to the host. And 11.9% of feed entries link to *another* host -- a blog
that moved domains, a link post, a sibling subdomain -- which cannot be written
as home + path, so those rows store the full URL. Feed links are resolved by
`resolve_link()` against the feed's own URL first, because feeds emit relative,
protocol-relative and scheme-less links and feedparser is given no base.

`topicMask` also carries flags: bit 13 feed-sourced, bit 14 kind-came-from-a-rule,
bit 15 link-is-dead.

`meta.json` also records `n_feed_urls` and `n_feed_posts`, purely so the next
build can be compared against this one. Every other check asks whether the index
is internally consistent, and a much smaller index is perfectly consistent: a
cold feed cache builds a valid index with 17.9% fewer posts, and the checks used
to wave it through. Pass the previous `meta.json` as a second argument to
`check_index.py` to enforce that.

## CI

Three workflows. `refresh-blog-index.yml` rebuilds monthly (and on demand) and
runs the index checks, the search eval and the browser suite on its own output
before committing it. `test.yml` runs the same suite on every push that touches
`blogs/` or `pipeline/` -- but not on the refresh bot's commits: GitHub triggers
no workflows for pushes made with `GITHUB_TOKEN`, which is why the refresh has to
test itself. `keep-caches-warm.yml` reads the refresh's two caches every week.
GitHub deletes a cache nobody reads for 7 days, so without it every monthly
refresh started cold, and a blog whose feed failed that day lost all its feed
posts until the next month.

## Classification is deliberately out of band

Stage 6 needs an LLM and is **not** run by CI. Its output is committed to
`classified/` and treated as an input. A newly discovered blog therefore stays out
of the index until the next classification pass — the correct failure mode, since
an unclassified blog has no `source`, and `source` decides what the newsroom
toggle hides.

To re-run it: `build_evidence.py` writes the batches, an agent labels each one to
`classified/cls_NNN.jsonl`, then `build_index.py` picks them up.

## Overrides

`overrides.json` is applied last and unconditionally. It corrects `source` where
the classifier was inconsistent between sibling domains (`sciencedaily.com` vs
`phys.org`; `tomshardware.com` vs `anandtech.com`) and denies hosting/paste
domains that are not blogs at all.

## Local run

```bash
python -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
OUT=work/hn.jsonl .venv/bin/python pipeline/pull_hn.py       # ~15 min, ~510 requests
.venv/bin/python pipeline/dedupe.py work/hn.jsonl work/dedup.jsonl
.venv/bin/python pipeline/aggregate_domains.py work/hn.jsonl work/cand.jsonl 3
.venv/bin/python pipeline/select_for_feeds.py work/cand.jsonl pipeline/classified work/targets.jsonl
.venv/bin/python pipeline/fetch_feeds.py work/targets.jsonl work/feeds.jsonl 16
FEED_CAP=12 .venv/bin/python pipeline/build_index.py \
    work/dedup.jsonl work/cand.jsonl pipeline/classified blogs/data \
    work/feeds.jsonl pipeline/dead_urls.txt
.venv/bin/python pipeline/check_index.py blogs/data
.venv/bin/python pipeline/sync_counts.py
.venv/bin/python pipeline/search_eval.py                     # needs playwright + chrome
.venv/bin/python pipeline/browser_test.py                    # needs playwright + chrome
```

To refresh link rot (slow, and optional — the committed `dead_urls.txt` stays
valid because dead links do not come back):

```bash
.venv/bin/python pipeline/check_links.py blogs/data work/linkcheck.jsonl 96
.venv/bin/python pipeline/distill_dead_urls.py work/linkcheck.jsonl blogs/data \
    pipeline/dead_urls.txt work/dead_hosts.json
```

It is resumable and keyed by URL, so it can be killed and restarted freely.
