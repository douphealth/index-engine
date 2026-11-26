# Index Engine

Finds every URL your sites publish and works out why each one is or isn't indexed. It pushes the indexable ones through every channel search engines accept, and gives you a ranked fix list for the rest. Results and controls are in a password-protected dashboard on Cloudflare that you can open from any device.

**Dashboard: https://index-engine-dashboard.papalexios.workers.dev** (the password is in `D:\Claude\SEO-Setup\Index-Engine-Dashboard-Login.txt`)

The dashboard is built as a Cloudflare Pages app (`dashboard/functions`, `dashboard/public`). In production it runs as a **Worker with static assets**, because the Cloudflare account is at its 100 Pages-project limit. It is the same code: `wrangler pages functions build` compiles `functions/` into one Worker (`dashboard/wrangler.worker.jsonc`), with `run_worker_first` so the password check runs before any file is served. If a Pages slot frees up, `npm run deploy:pages` deploys it to Pages unchanged.

**Adding a website:** Settings → Websites → type the domain(s) → Add. No code change: the PC adds it to its site list on the next tick and the first full run discovers every URL. For full data, also add the engine's Google service account as a user of the site's Search Console property, and add the site to Bing Webmaster Tools. Link and cleanup work needs a WordPress login on the PC (`engine/wp.py`).

## How it fits together

```
your PC (Windows task, every 10 min)                    Cloudflare: index-engine-dashboard (Worker + assets)
  indexengine.py tick  ── pulls decisions + queued runs ──►  /api/engine/*   (engine token)
     │ runs the engine with YOUR credentials                 D1: settings, ignored URLs, requests done,
     │ (Google service account, Bing key, WordPress)             fixes, run queue, run history
     └── uploads results after each run ───────────────►  KV: summary, per-site URL tables, last log
                                                         you, in a browser (password)  ◄── dashboard/
```

- **Credentials never leave the PC.** The dashboard only receives results.
- **Commands from the dashboard wait for the PC.** "Run now" is picked up within 10 minutes whenever the PC is on.
- **The repo holds code only.** Data, logs, backups and secrets are git-ignored. The credential files live in `D:\Claude\SEO-Setup\`:
  - `Index-Engine-Dashboard.json`: dashboard URL and engine token
  - `Index-Engine-Dashboard-Login.txt`: your dashboard password
  - `Index-Engine-Cloudflare-Token.txt`: the deploy token, limited to Pages, D1 and KV

## What makes it smart and autonomous

- **Speed without changing a decision (since 26 Nov 2026; `engine/memo.py`).**
  - **The analysis** that follows every run, and every quick tick that found a new post, took about 167 seconds; it now takes about 31 seconds. Three scans re-read every stored post each time (the template scan, the cleanup scan and the link search). Each post's result is now stored with a digest of the content it came from and of the code version that computed it, and is recomputed only when either changes. Checked on the live data of 26 Nov 2026 (8,383 pages, 22 sites): the same 321 cleanup proposals, the same 77 link proposals and the same bucket for every page, before and after the speed change alone; a first run and a repeat run agree exactly. The first run after an update, or after a post changes, pays for the posts that changed.
  - **Google inspection** keeps 12 calls in flight per property instead of 4 (measured on one property: 1.31 URLs a second against 0.53) and shares one access token between threads instead of fetching one per thread. Pacing still keeps every property under Google's 600 a minute.
  - **The audit** starts every site at 3 requests in flight. A site that keeps answering within 0.8 s earns one more at a time, up to 8; a failed fetch, a 429 or 5xx or a bot challenge halves it at once and caps it for the rest of the run. A site that is simply slow is read exactly as before, and a site listed in `SLOW_HOSTS` keeps its fixed number.
  - **The dashboard** answers a request for results that have not changed with an empty 304, so a reload draws the page from the copy the browser already has instead of downloading a site's whole URL table again.
  - Every run's log ends with a `[time]` line: where the time went, slowest stage first.
- **Recovery: is each engine still showing the site at all? (since 19 Nov 2026; `engine/recovery.py`, Recovery page).** The traffic verdicts compare 28 days with the 28 before, so a site that collapsed months ago reads as "stable at almost zero" and kept getting submissions and request lists that cannot help it.
  - **Visibility.** The latest 14 days of impressions per engine against the site's own usual level: the 80th percentile of its 28-day averages over the past ~13 months, with the last six weeks left out. Below 20% of usual is *collapsed*, below 50% a *severe drop*. The date of the biggest step down (14 days before vs after) is reported so it can be matched to a Google update or a site change. A site that never had 10 impressions a day is not judged.
  - **What a collapse changes.** It becomes the first urgent priority and the Action Center's traffic step, with direct links to Search Console's Manual actions, Security issues and Page indexing reports (no API exposes these). The site's "Request indexing" quick win is dropped: requests cannot reverse a site-wide judgment.
  - **Prune plan.** Every indexable post and page gets one decision with its evidence: *keep* (earns clicks or ≥100 impressions on either engine, or is the home page/a hub), *fix* (has demand but is flagged), *merge* (near-copy of a stronger page; target given), *redirect* (no search value but other sites link to it; closest indexed page given), *noindex* (no clicks and under 20 impressions on both engines, at least 120 days old, and thin, flagged or left out of Google's index), *review* (the rules do not decide). Downloadable as CSV and written to `out/prune_plan.csv`. Nothing is applied.
  - **Inbound links.** Bing's `GetLinkCounts` is read weekly per site, so a page other sites link to is redirected, never just noindexed. Until it has been read, the page says so.
  - **Scaled-content signals (flag `TEMPLATE_ARTIFACT`).** From the stored WordPress content: runs of 3+ headings that differ only by a number and say more than "Week N" ("Use-case notes for … #1 … #7"), pipeline leftovers ("WordPress Media Library", "Image generation failed", "as an AI language model", "[insert …]"), and headings repeated on 25%+ of a site's posts that are not ordinary section names (FAQ, Conclusion, Key takeaways and similar are ignored). On 19 Nov 2026 live gearuptofit and affiliatemarketingforsuccess articles carried all three while Google showed only their home pages.
- **Autonomous mode (since 25 Nov 2026; one switch on the Action Center and in Settings, off by default).** With it on, the PC applies proposed internal links and content cleanups without waiting for your click.
  - **Limits:** the daily caps still apply (links and cleanups per site per day). Pause in Settings stops everything, Autonomous mode included.
  - **Brakes per site:** a canary (until one cleanup is confirmed gone from the live page, at most one autonomous cleanup a day) and a circuit breaker (two failed autonomous cleanups within 24 hours stop autonomous cleanups on that site for 24 hours and raise an urgent alert).
  - **gearuptofit** stays manual unless you tick "Include gearuptofit", because its AGENTS.md asks for your click on every change.
  - **Never automatic:** noindexing, redirecting, merging or deleting pages, writing or rewriting text, Search Console's "Request indexing", and Google's Indexing API.
  - **Where to see it:** the Action Center panel shows its state, what it applied in the last 24 hours and any stop. Every autonomous change is logged as "Autonomous mode" and can be undone like an approved one.
- **Cleanup queue: remove what Google's scaled-content systems judge (since 25 Nov 2026; `engine/cleanup.py`, Recovery page).** Recovery finds the scaffolding; this removes it, one approved post at a time.
  - **What is cut:** whole sections (a heading and everything up to the next heading of the same or a higher level). These are numbered template sections ("Use-case notes for … #1–#7"), pipeline leftovers ("Visual Examples From the WordPress Media Library") and boilerplate blocks whose text is near-identical (80%+ shingle overlap) on 5 or more posts of the site. A section with a repeated heading but its own content is kept. Nothing else on the post changes.
  - **What is never cut:** a section that cannot be removed cleanly (it would leave an HTML element or a Gutenberg block open), a last section over 300 words, any section over 1,500 words, or ordinary section names (FAQ, Conclusion, …).
  - **How it is applied:** only after your approval on the Recovery page (no autopilot), at most `cleanups_per_site_per_day` posts per site a day (default 10, max 30; Settings). The post is re-read and the sections found again by heading, the raw content is backed up (`out/backups/cleanup/`), and WordPress must store the cut or the original is restored at once. The live page (cache bypassed) must then show each cut heading fewer times than before. When it does not yet (page cache, or a page built with Elementor that ignores the post body), it is re-checked every tick and the result says so after two days. gearuptofit gets its CHANGELOG-AGENTS.md entry and a site_guard check after the batch. Undo restores the backup unless the post was edited since.
- **Revival: traffic is the goal, indexation is a means.** The engine keeps the daily history both engines publish and says, with the evidence, where each site's clicks are moving and why that is hard to read.
  - **Sources.** Bing: `GetRankAndTrafficStats` (daily clicks and impressions, about 16 months), `GetCrawlStats` (daily: pages in Bing's index, pages crawled, errors, robots blocks, 4xx/5xx) and the per-page daily rows of `GetPageStats`. Google: Search Analytics by date and by page.
  - **Windows.** The latest 28 days of data against the 28 before, ending on the last day each engine reported, not on today.
  - **A change is called real only when** it is at least 25% and more than 3 standard errors for count data (the square root of the two totals added), with at least 50 clicks in one window. Anything smaller is "too little data", however large the percentage. Daily traffic is noisier than a pure count, so "medium" confidence is indicative, not proof.
  - **Where the lost clicks went.** Per page, lost clicks are split into pages that are blocked (noindex, redirect, canonical, robots, errors), removed (404/410), redirected, live but unlisted, and live and indexable. The page rows say how much of the site's clicks they explain, because the engines report some low-volume traffic only at site level.
  - **Spelling variants.** Engines often report a page under the other spelling (with or without the trailing slash). Those clicks are credited to the listed URL, which also fixes page values the rest of the engine uses. A variant that answers 200 instead of redirecting is reported as a duplicate-URL finding.
  - **Bing's index.** "Pages in index" is Bing's own figure. A site with far more pages in Bing's index than URLs the engine can find (5 times or more, over 1,000 pages) is flagged: that often means duplicate, parameter or archive URLs. Robots blocks, server errors and missing pages are flagged by rate, not by raw count.
  - **Every statement is an association measured on the engines' data.** The engine does not claim why a page lost clicks.

- **An indexation-odds model trained on your own sites.** Every page Google has judged teaches the model what gets pages indexed on these sites.
  - It is an L2 logistic regression with 5-fold cross-validation.
  - Cross-validation computes each site's rate from the training fold rather than its held-out labels. Other features, including links from indexed pages, still use current portfolio-wide verdicts, so the reported AUC can be optimistic and is not proof of future accuracy.
  - Model drivers vary between runs; associations do not prove that changing a feature will change indexing.
  - Each page gets odds plus a plain-English "what holds it back". If the data can't support a model, it says so and falls back to fixed estimates.
- **Best next actions.** The model simulates each action and ranks them portfolio-wide by expected gain (page value × rise in odds). The actions are: add links from indexed pages, request indexing, de-duplicate, add substance, source claims. The request-indexing list is ranked the same way.
- **Internal-link autopilot.**
  - The engine keeps a local copy of every published post's content (read in bulk over the WordPress REST API, then only what changed). For every page where the odds model expects links to help, best expected gain first, it looks for the page's name in the article text of every page Google indexes or that earns Bing clicks.
  - An anchor qualifies only when it is a whole phrase of the target's title (never a fragment across punctuation, a compound cut short, or what the title says the page is *not* about). It must carry the title's main subject, and no other title or slug on the site may contain it. It must sit inside a sentence, not a heading, label, list entry, link, caption, table or shortcode, and it must not be a stock phrase found all over the site. Plurals, possessives and HTML entities are matched.
  - Since 26 Nov 2026 a page is also named by its main heading (H1) where that is worded differently from the title, under the same rules: a whole phrase, found in no other page's title, heading or slug, and sharing a topic word with the title. The heading may only reword the page's name: every topic word of the phrase must also be in the title or the address ("burnout symptoms" for a page titled "Burnout at Work" at `/burnout-symptoms/`; never "turn goals" from "How to Turn Goals Into Finished Results"). No phrase, from a title or a heading, stops right before "a", "an" or "the" ("run a mile", never "take to run"). On that day's data, with the next rule, the proposals went from 77 for 44 pages to 83 for 50 pages.
  - Among the trusted pages whose text contains the phrase, one on the target's own subject is preferred: its name shares topic words with the target's, rare words counting for more. A very strong unrelated page can still win, and each proposal says when the linking page is on the same subject. Where the text has the title's own leading word in front of the phrase ("New Balance Fresh Foam X", not "Balance Fresh Foam X"), the anchor takes the full name.
  - Each run proposes at most 3 links per page, 2 per source page and 60 per site. On 24 Oct 2026 this found 242 links for 132 stuck pages; the old title-only matcher found 27.
  - You approve on the Links page; your PC applies them within 10 minutes, at most N per site per day (default 5, max 10).
  - Every link is backed up, verified in WordPress and on the page visitors get, logged, and can be undone.
  - Sites behind Cloudflare keep serving the old copy of a page for up to about 4 hours. After each link change the engine clears the cache of just that page with its own token (`Index-Engine-Cloudflare-Token.txt`, needs **Zone · Cache Purge**; it never tries any other credential). Without that permission the card says when visitors and Google will see the link, and every tick re-checks until they do.
  - Autopilot is on unless you switch it off in Settings (since 6 Nov 2026): the best-ranked proposals are applied without waiting for approval, within the daily cap per site. It never applies to gearuptofit, whose rules require your click.
- **Measured results, and the engine learns from them (since 12 Nov 2026; `engine/results.py`, Results page).** Every page that received links from the engine is measured once, 31 days after its first link:
  - **A page that already ranked:** Google clicks, impressions and average position over the 28 days before against the 28 days after. Its change is compared with the median change of the 5 most similar pages of the same site (by impressions before) that got no link in those weeks, so seasonal and site-wide swings cancel out. Where the anchor is a search the page shows for, that search's position before and after is shown too.
  - **A page that was not indexed:** whether Google indexed it within 30 days, against the share of the site's other unindexed, unlinked pages indexed in the same 30 days.
  - **What it learns:** after 15 measured pages of a kind, the measured effect scales how that kind of link is ranked (×1.5 when it clearly beat the comparison pages, ×0.5 when it did not), so the autopilot spends its daily link budget on what works on these sites.
  - **Cost and limits:** the measurement is read-only. It costs two Search Analytics calls per window (shared by every page of a property) plus two per page that already ranked. These are observational comparisons, not randomized experiments, and the page says so. AI-answer citations (Bing's AI Performance report) have no API as of this writing, so they cannot be measured here.
- **Growth links (since 6 Nov 2026).** A page that is indexed and ranks around positions 4–15 for a search (the Growth page's "close to the top") also gets internal-link proposals. Since 26 Nov 2026 so does a page Bing ranks there while Google has not indexed it yet. The anchor is that search's own wording, taken from trusted pages whose text already says it. Guards:
  - the search must name the page (share a word with its title);
  - no other page's title or slug may contain it;
  - no other page of the site may rank better for it, so a link never feeds a competing page;
  - one link per source and target.

  Each proposal says why on the Links page, and the Growth page shows each page's links applied and waiting. With Autopilot on they are applied like the others.
- **Fresh-content autopilot.** Every 10-minute tick checks each WordPress site for posts published or updated since the last look. New ones are audited, inspected, gated and submitted within minutes. Google and Bing are then told the sitemap and feed changed, but only after the engine has re-read them and seen the new post (or its new version) listed. A cached sitemap is never resubmitted with old content; the daily run picks it up instead.
- **Sitemaps and feeds for both engines** (`engine/sitemaps.py`).
  - Every sitemap root and the WordPress RSS feed (`/feed/`) are registered with Search Console and, since 5 Nov 2026, with Bing (`GetFeeds` / `SubmitFeed`). Google accepts an RSS or Atom feed as a sitemap. The XML sitemap is the full inventory; the feed carries recent changes.
  - A registered sitemap is resubmitted when URLs were **added or updated** since its last submission, at most once an hour. Before this, only a rise in the URL count triggered it, so publishing 3 posts and removing 3, or updating posts, told Google nothing.
  - After a 401/403 (no permission) a sitemap is retried after 7 days; after other errors, after a day (4xx) or an hour (5xx or network).
  - When the feed declares a WebSub hub (`<atom:link rel="hub">`), the hub is told the feed changed. Without a hub nothing is pinged, and the site diagnosis says a WebSub plugin would add one.
  - **lastmod trust.** Google uses lastmod only when it is consistently accurate. The engine flags four problems: the same lastmod on 80% or more of URLs (the time the file was generated), lastmod in the future, most URLs missing lastmod, and posts updated in WordPress more than a day after their sitemap entry says (a cached sitemap). Where lastmod cannot be trusted, only new URLs count as changes.
  - None of this forces indexing. It makes sure each engine knows about every URL and every change quickly, and does not waste a crawl on a stale list.
- **Self-update with automatic rollback** (`engine/update.py`). At most once an hour the tick fetches `origin/main`. It adopts the new code only when:
  - this folder is a clean git checkout on `main`;
  - the new commit is a fast-forward;
  - every requirement is installed;
  - the candidate passes the unit tests and the selftest on this PC, in a separate git worktree, with no credentials visible to it.

  A commit that fails is never tried again. If two runs fail on the new code before any succeeds, the previous commit is restored. Each step raises a dashboard alert, and the dashboard shows the running version. Turn it off in Settings > Engine updates. `python indexengine.py update` checks now; `tick --no-update` skips it.
- **Regression alarms.** You get an alert when:
  - an indexable page turns noindex, 404s, redirects or loses its canonical;
  - content shrinks sharply;
  - a sitemap drops 10% or more of its URLs;
  - a site stops answering;
  - a page enters or leaves Google's index.
- **Trust-risk scanner.** It flags phrasings that read as invented first-hand experience, ratings or statistics (guardrail 4), with the exact sentences. Since 28 Oct 2026 it also catches quotes credited to a named expert with a job title or institution (not only a year), unverifiable bylines, placeholder names ("John Doe"), invented experiments ("I split 68 volunteers") and invented testing panels. On one real site this roughly tripled the number of flagged posts. A match is a prompt to verify: a real citation with a named source is fine, an invented quote is not. The scanner never edits anything.
- **Page-weight check.** The audit records the HTML bytes the server sends and how much of it is base64 image data pasted into the page or its schema. A page over 1 MB, or carrying more than 20,000 characters of pasted image data, gets the `HEAVY_PAGE` flag, a site-level warning and a plain-English fix. (A `data:` URI is also not a valid image URL in structured data.)
- **Search opportunities (Growth page).** Indexation is a means; clicks are the goal. The engine reads which searches each page already shows for: Google's query report for every page in one call per site (daily run), and Bing's per-page query report, a few pages per idle tick (about 2 s each, highest-impression pages first, each page at most once a week, stopping at the first throttle). From that it lists, per page:
  - *Close to the top*: positions about 4 to 15 for a search with real impressions. Estimated gain = the clicks the site's own click-through at position 3 would bring, minus what the page gets now. For a search of up to six words the advice also names the search's topic words that neither the page's title nor its main heading says ("poop" for *mouse poop* on a page titled "What Do Mouse Droppings Look Like?"), when there are at most three: a specific gap the owner can check and close.
  - *Ranks well, rarely clicked*: already in the top ~5 but clicked at under half the site's usual rate for that position, and the shortfall has under a 2% chance of being luck (exact binomial test). The advice names the current title length and whether a meta description exists.
  - *Your pages compete with each other*: two or more of the site's pages share one search's impressions and none is in the top 3.

  Click-through by position is learned per site and engine, pulled toward the portfolio's where data is thin, and never allowed to rise as the position worsens. Searches for the site's own name (from the domain and the title suffix) and searches with no letters ("17-8") are left out. One row per page, with its searches listed. Nothing is changed automatically: these are the owner's edits.
- **AI and search crawler access.** Daily, read-only: robots.txt plus a request to the home page in the name of each AI search crawler (OAI-SearchBot, ChatGPT-User, PerplexityBot) and training crawler (GPTBot, ClaudeBot), compared with a normal browser. Search crawlers (which decide whether a site can be found and cited) and training crawlers (blocking them is a legitimate choice) are kept apart. A change raises an alert; a blocked search crawler is an urgent priority. A 200 is conclusive; a block at the edge is evidence, because the request comes from this PC and not from the crawler's network. Cloudflare's own bot setting is not read: the app token has no Bot Management permission.
- **Structured-data check.** The audit counts each page's JSON-LD blocks and those that do not parse; an indexable page with a broken block gets the `SCHEMA_ERROR` flag and a technical priority. It also stores the meta description, which the click-through advice uses.
- **Observed outcomes, not causal impact.** It tracks Google index transitions over 30 days and summarizes the last known status of older requests, links and submissions. The action summaries cover historical records, include missing or stale verdicts, and do not prove an action caused indexing. The request summary is also used as a heuristic in the model.
- **Follow-up inspections.** Pages you requested or that received links are re-inspected 2–3 days later. These observations can help monitor changes but cannot establish whether an action caused them.

## Commands

```
python D:\Claude\SEO-Setup\index-engine\indexengine.py tick
```

`tick` is what the scheduler runs every 10 minutes. It sends a heartbeat to the dashboard, runs any queued run, and otherwise runs the daily run once a day after the time set in the dashboard (default 10:30).

| Command | What it does |
|---|---|
| `run` | Everything, all enabled sites, now |
| `run --dry-run` | Decides everything and logs what it *would* submit; submits nothing |
| `run --no-submit` | Measures and reports only |
| `run --site frenchyfab.com` | One site (repeat `--site` for more) |
| `run --inspect-max 200` | Caps URL Inspection calls per property this run |
| `publish` | Re-uploads the latest results to the dashboard without crawling |
| `status` | One line per site from the last run |
| `update` | Check `origin/main` now and adopt it if it passes the tests on this PC |
| `selftest` | Offline end-to-end test: a fake site plus fake Google, Bing, IndexNow and a WebSub hub (129 checks); runs in GitHub Actions on every push |

## The dashboard (`dashboard/`)

| Page | What you can do |
|---|---|
| Recovery | Whether Google and Bing still show each site (against its own usual level), the date of the step down, the checks that rule out a manual action or technical block, the headings repeated across a site's posts, and the per-page prune plan with CSV |
| Overview | **Your priorities**: one ranked to-do list (urgent problems, then quick wins by expected pages indexed per minute, then technical fixes, then content work), each with its expected gain, effort and a one-click link. Also: where pages get stuck (found → indexable → known to Google → crawled → indexed), the indexed-share trend, top pages, last-30-days impact, alerts, and every site with its next step |
| Revival | Per-site traffic verdicts with confidence (Bing and Google), a daily-clicks chart, where the lost clicks went by kind of page, Bing's crawl report and its findings, and the pages to recover first with a specific next step for each |
| Plan | What decides indexing on your sites (the model's drivers and accuracy) and every action ranked by expected gain, with one-click handling |
| Links | The internal-link approval queue: each proposal shows the anchor highlighted in its sentence. Approve, reject, cancel or undo |
| Alerts | Regressions and Google gains and drops, with acknowledge |
| Site | Diagnosis, filterable URL table (by state, flag or search), why each page isn't indexed and what to do. Per URL you can open the page, inspect it in Search Console, **ignore** it (never submitted, off the work lists) or add a note. Also lists the internal links to add |
| Requests | Today's "Request indexing" list with direct Search Console links. Mark each one **done**; it stays off the list for 14 days |
| Fixes | Open fixes with the exact command or wp-admin step. Mark them done or skipped |
| Runs | Queue, history with totals, and the last run's log |
| Settings | Enable or disable sites, **pause everything that changes something** (submissions and link edits), daily run time, request-list size, and an inspection cap |

**Run now** (top right) queues a run for one site or all sites. It can be set to measure only, re-fetch every page, or dry-run.

**Stack:** a static app (no framework, no build step) with Pages-style Functions; D1 for state, KV for results. Signing in uses a password and a signed HttpOnly cookie, with login rate limiting; the engine uses its own bearer token.

Pushing to `main` deploys through `.github/workflows/deploy-dashboard.yml`, which applies D1 migrations, builds and deploys, then smoke-tests the auth wall. The workflow uses repo secrets `CLOUDFLARE_API_TOKEN` (a token limited to Pages, D1, KV and Workers Scripts on one account) and `CLOUDFLARE_ACCOUNT_ID`.

To deploy by hand:

```
cd dashboard
npm run deploy
```

This needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the environment.

**Local development:**

```
cd dashboard
npm ci
npx wrangler d1 migrations apply index-engine --local
npx wrangler pages dev --port 8788
```

Local secrets go in `dashboard/.dev.vars`: `DASHBOARD_PASSWORD`, `SESSION_SECRET` and `ENGINE_TOKEN`.

**Rotating secrets:**

```
npx wrangler secret put <NAME> --config wrangler.worker.jsonc
```

- Changing `SESSION_SECRET` signs everyone out.
- Changing `ENGINE_TOKEN` also requires the new token in `Index-Engine-Dashboard.json`.

The first run fetches every page (about 4,000 URLs across the 10 sites on 23 Oct 2026, roughly 10 minutes; the 12 sites added on 25 Nov 2026 add about 4,600 sitemap URLs, about 3,300 of them on tenderatlas.shop). Later runs only re-fetch pages that changed or are older than 7 days.

## What happens on each run

1. **Discover.** It reads robots.txt, every sitemap (recursing through indexes, ignoring `image:loc`) and, on WordPress, the public REST API. A published post that no sitemap lists gets flagged.
2. **Audit.** It fetches each URL the way a crawler does and records:
   - status, redirects, `noindex` (meta tag or header), and robots.txt rules for Googlebot and bingbot
   - the canonical target and the main-content word count
   - a content fingerprint, used to find near-duplicates
   - every internal link, used to find orphans and pages linked only from menus
3. **Traffic history** (Bing and Google): the daily series, Bing's crawl report and the per-page before/after windows, then a handful of live requests to URLs that earned clicks but are unlisted or are spelling variants of listed pages.
4. **Google** (needs the service account):
   - Any page with a Search impression in the last 14 days counts as indexed, so it costs no quota.
   - Everything else goes through the **URL Inspection API**: 2,000 per property per day, never-checked and highest-value URLs first.
   - Each URL is re-checked only when its state is stale (7–45 days, depending on the state) or the page changed.
5. **Bing** (needs the Bing key). It pulls page traffic, so pages that earn Bing clicks are protected, and checks crawl status for submitted URLs.
6. **Submit.**
   - **IndexNow** where the site hosts a key file. One call reaches Bing, Yandex, Seznam, Naver and Yep.
   - Otherwise the **Bing URL Submission API**, within the daily quota Bing reports.
   - **Sitemaps and the RSS feed, to Search Console and Bing:** each is registered once, then resubmitted only when URLs were added or updated since its last submission (at most hourly). A WebSub hub, if the feed declares one, is notified too.
   - Only URLs that are indexable right now are sent: 200 status, no redirect, no `noindex`, self-canonical, allowed by robots.txt.
   - Each URL goes out once per channel, and again only when its `lastmod` changes.
7. **Triage, revival and report.** Every URL gets one bucket and one concrete action, every site gets its traffic verdict and revival picture, and clear changes raise alerts (traffic: weekly at most; Bing findings: monthly at most). See `out/`.

## What it deliberately does not do

- **Google Indexing API.** Google limits it to job-posting and livestream pages and says misuse can revoke access ([quickstart](https://developers.google.com/search/apis/indexing-api/v3/quickstart), updated 16 Oct 2026). Tools and services that use it for blog posts are breaking that rule. On sites Google already rates low, that is a risk with no upside.
- **Clicking "Request indexing" for you.** There is no API for it, and automating the Search Console UI is bot activity. The app gives you a short daily list (`request_indexing_today.csv`) with direct links instead. It includes only URLs where a request can help: Google has never seen the page, the page has been fixed since Google's last look, or it changed after Google crawled it.
- **Editing your sites.** It recommends; you or an agent apply changes one batch at a time (see `PORTFOLIO-GUARDRAILS.md`).

## Output (`out/`)

| File | Use it for |
|---|---|
| `report.html` | Open this first. Per-site diagnosis, highest-value fixes, today's request list, link suggestions |
| `fix_technical.csv` | Blocked URLs (4xx/5xx, redirects in sitemaps, `noindex`, robots, canonical elsewhere, missing from sitemap). The only bucket a fix is guaranteed to clear |
| `google_not_indexed.csv` | Indexable pages Google declines, with the reason and what would change it |
| `internal_link_plan.csv` | For each stuck page: up to 3 related pages Google already indexes, or that earn Bing clicks, that should link to it |
| `request_indexing_today.csv` | Up to 10 URLs per site for the Search Console button, with direct links |
| `near_duplicates.csv` | Pairs whose main text overlaps by 50% or more |
| `prune_plan.csv` | One decision per indexable page (keep, fix, merge, redirect, noindex, review), with the evidence and the target for merges and redirects |
| `urls.csv` | Every URL with bucket, flags, reason, action and value score |
| `summary.csv` | Per-site counts, plus the change in indexed pages since the first run |
| `index_engine.db` | All state (SQLite). Delete it to start over |
| `logs/` | One log per run. Secrets are scrubbed |

## Reading the buckets

| Bucket | Meaning | What changes it |
|---|---|---|
| Indexed | Google serves it | — |
| Crawled, not indexed | Google read the page and judged it not worth indexing | Changing the page: unique substance, merging with a stronger page, or removal. Re-submitting does nothing |
| Discovered, not crawled | Google knows the URL but rates it (or the site) low priority | Links from pages Google already indexes; site-wide quality |
| Unknown to Google | Never seen | A sitemap Google reads, internal links, then a request |
| Fixed since Google's last look | Google saw a 404, `noindex` or redirect that is now gone | Request indexing |
| Google picked another URL | Treated as a duplicate | Make the page distinct or merge (check Bing clicks first) |
| Technical buckets | The page tells engines not to index it, or is broken | Fix it, or drop it from the sitemap |

When most checked pages on a site are "crawled/discovered, not indexed", the report says so plainly. That is Google judging the whole site, and no submission tool changes it.

## Credentials

It reads `D:\Claude\SEO-Setup\GSC-Bing.txt`, the same file `export_gsc_bing.py` uses. Credentials are never printed or written anywhere.

- **Google service account:** needs access to each Search Console property.
  - *Restricted* is enough for inspection and performance data.
  - Submitting sitemaps needs *Full* or *Owner*. Without it the log says so, and nothing else breaks.
- **Bing key:** the Bing Webmaster Tools API key. The file holds two key-shaped values, and on 23 Oct 2026 the first one was stale (`InvalidApiKey`). The app tests each candidate and uses the one Bing accepts.

Without credentials the app still discovers, audits, submits through IndexNow where a key file exists, and reports.

## IndexNow setup for a site without a key file

**Optional for this portfolio.** On 23 Oct 2026, Bing's URL Submission API allowed 10,000 URLs/day on six sites, 200 on gearuptogrow and 100 on openclaw. That covers every URL, and IndexNow only adds Yandex, Seznam, Naver and Yep. Only affiliatemarketingforsuccess has a key file (`/indexnow-key.txt`).

**imagealchemy.app** is in neither the Bing account nor the service account's Search Console access. To cover it:
1. Add it in Bing Webmaster Tools (Import from Google Search Console).
2. Add the service account as a user of its Search Console property.

The next run picks it up.

If you still want IndexNow on a site, pick one option:

- **Rank Math, the official "IndexNow" WordPress plugin by Microsoft Bing, or Cloudflare Crawler Hints:** turn it on. The plugin then pings on every publish or update. For the app to submit too, add the key here as well.
- **Manual setup:**
  1. Generate a key, for example: `python -c "import uuid;print(uuid.uuid4().hex)"`
  2. Upload a file named `<key>.txt`, containing only the key, to the site root.
  3. Add it to `indexnow_keys.json` next to `indexengine.py`:

```json
{"example.com": {"key": "<key>"}}
```

The app verifies the file is live before using it.

## Run it every day (Windows Task Scheduler)

The task **"Index Engine"** is already registered. It runs `indexengine.py tick` every 10 minutes with:
- `C:\Python312\python.exe` and the working directory set to this folder
- a catch-up start if the PC was off
- permission to run on battery
- a 2-hour limit
- a lock file so runs never overlap

`tick` decides whether anything needs doing. A queued dashboard run goes first; otherwise the daily run happens once, after the time set in the dashboard.

**What it guarantees when things go wrong**
- **Pause and paused sites are honoured even offline.** The engine keeps the last settings the dashboard gave it. If the dashboard cannot be reached, a paused engine stays paused and a disabled site stays untouched, instead of the settings silently lapsing.
- **A paused site is never touched.** Not by a scheduled run, a queued run (the dashboard marks it "skipped: paused in Settings", not "done"), a link edit or the fresh-content check. With everything paused, approved links and undo wait too, and the Links page says so.
- **One run at a time, and a crash cannot block the next.** The lock is taken atomically. A lock left by a crashed or killed run (its process is gone) is taken over at once; a busy lock is not a failure.
- **A failed daily run is retried** (30 minutes apart, at most 3 times a day) instead of costing the day. A run stuck "running" for more than 4 hours counts as dead.
- **A crashed quick tick loses nothing.** It is recorded as failed, the lock is released, and the new-post watermark has not moved, so the next tick picks the same posts up again.
- **A run the PC never finished does not block the queue.** The dashboard closes a run that has been "running" for 3 hours as failed, so you can queue another.
- **Evidence is never overwritten by an error.** A failed Google inspection keeps the last real verdict and leaves no history row; a Bing lookup is only recorded after Bing answered for that URL.
- **Bing's lookup limit is shared fairly.** `GetUrlInfo` is throttled per account, so the sites take turns, calls are paced, and the first throttle ends the check until the next run.

To check it:

```
schtasks /Query /TN "Index Engine" /V /FO LIST
```

Google's inspection quota resets at midnight Pacific time, which is 10:00 in Greece for most of the year (09:00 for a week or two around the DST changes). A 10:30 run starts with a full day of quota. A daily run covers a 2,000-page site in 1–2 days, then keeps every page's state current.
