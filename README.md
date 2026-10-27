# Index Engine

Finds every URL your sites publish and works out why each one is or isn't indexed. It pushes the indexable ones through every channel search engines accept, and gives you a ranked fix list for the rest. Results and controls are in a password-protected dashboard on Cloudflare that you can open from any device.

**Dashboard: https://index-engine-dashboard.papalexios.workers.dev** (the password is in `D:\Claude\SEO-Setup\Index-Engine-Dashboard-Login.txt`)

The dashboard is built as a Cloudflare Pages app (`dashboard/functions`, `dashboard/public`). In production it runs as a **Worker with static assets**, because the Cloudflare account is at its 100 Pages-project limit. It is the same code: `wrangler pages functions build` compiles `functions/` into one Worker (`dashboard/wrangler.worker.jsonc`), with `run_worker_first` so the password check runs before any file is served. If a Pages slot frees up, `npm run deploy:pages` deploys it to Pages unchanged.

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

- **An indexation-odds model trained on your own sites.** Every page Google has judged teaches the model what gets pages indexed on these sites.
  - It is an L2 logistic regression with 5-fold cross-validation.
  - Cross-validation computes each site's rate from the training fold rather than its held-out labels. Other features, including links from indexed pages, still use current portfolio-wide verdicts, so the reported AUC can be optimistic and is not proof of future accuracy.
  - Model drivers vary between runs; associations do not prove that changing a feature will change indexing.
  - Each page gets odds plus a plain-English "what holds it back". If the data can't support a model, it says so and falls back to fixed estimates.
- **Best next actions.** The model simulates each action and ranks them portfolio-wide by expected gain (page value × rise in odds). The actions are: add links from indexed pages, request indexing, de-duplicate, add substance, source claims. The request-indexing list is ranked the same way.
- **Internal-link autopilot.**
  - The engine keeps a local copy of every published post's content (read in bulk over the WordPress REST API, then only what changed). For every page where the odds model expects links to help, best expected gain first, it looks for the page's name in the article text of every page Google indexes or that earns Bing clicks.
  - An anchor qualifies only when it is a whole phrase of the target's title (never a fragment across punctuation, a compound cut short, or what the title says the page is *not* about). It must carry the title's main subject, and no other title or slug on the site may contain it. It must sit inside a sentence, not a heading, label, list entry, link, caption, table or shortcode, and it must not be a stock phrase found all over the site. Plurals, possessives and HTML entities are matched.
  - Each run proposes at most 3 links per page, 2 per source page and 60 per site. On 24 Oct 2026 this found 242 links for 132 stuck pages; the old title-only matcher found 27.
  - You approve on the Links page; your PC applies them within 10 minutes, at most N per site per day (default 5, max 10).
  - Every link is backed up, verified in WordPress and on the page visitors get, logged, and can be undone.
  - Sites behind Cloudflare keep serving the old copy of a page for up to about 4 hours. After each link change the engine clears the cache of just that page with its own token (`Index-Engine-Cloudflare-Token.txt`, needs **Zone · Cache Purge**; it never tries any other credential). Without that permission the card says when visitors and Google will see the link, and every tick re-checks until they do.
  - Optional auto-approve stays off by default and never applies to gearuptofit.
- **Fresh-content autopilot.** Every 10-minute tick checks each WordPress site for posts published or updated since the last look. New ones are audited, inspected, gated and submitted within minutes.
- **Regression alarms.** You get an alert when:
  - an indexable page turns noindex, 404s, redirects or loses its canonical;
  - content shrinks sharply;
  - a sitemap drops 10% or more of its URLs;
  - a site stops answering;
  - a page enters or leaves Google's index.
- **Trust-risk scanner.** It flags phrasings that read as invented first-hand experience, ratings or statistics (guardrail 4), with the exact sentences.
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
| `selftest` | Offline end-to-end test: a fake site plus fake Google, Bing and IndexNow (74 checks); runs in GitHub Actions on every push |

## The dashboard (`dashboard/`)

| Page | What you can do |
|---|---|
| Overview | **Your priorities**: one ranked to-do list (urgent problems, then quick wins by expected pages indexed per minute, then technical fixes, then content work), each with its expected gain, effort and a one-click link. Also: where pages get stuck (found → indexable → known to Google → crawled → indexed), the indexed-share trend, top pages, last-30-days impact, alerts, and every site with its next step |
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

The first run fetches every page (about 4,000 URLs across the 10 sites on 23 Oct 2026, roughly 10 minutes). Later runs only re-fetch pages that changed or are older than 7 days.

## What happens on each run

1. **Discover.** It reads robots.txt, every sitemap (recursing through indexes, ignoring `image:loc`) and, on WordPress, the public REST API. A published post that no sitemap lists gets flagged.
2. **Audit.** It fetches each URL the way a crawler does and records:
   - status, redirects, `noindex` (meta tag or header), and robots.txt rules for Googlebot and bingbot
   - the canonical target and the main-content word count
   - a content fingerprint, used to find near-duplicates
   - every internal link, used to find orphans and pages linked only from menus
3. **Google** (needs the service account):
   - Any page with a Search impression in the last 14 days counts as indexed, so it costs no quota.
   - Everything else goes through the **URL Inspection API**: 2,000 per property per day, never-checked and highest-value URLs first.
   - Each URL is re-checked only when its state is stale (7–45 days, depending on the state) or the page changed.
4. **Bing** (needs the Bing key). It pulls page traffic, so pages that earn Bing clicks are protected, and checks crawl status for submitted URLs.
5. **Submit.**
   - **IndexNow** where the site hosts a key file. One call reaches Bing, Yandex, Seznam, Naver and Yep.
   - Otherwise the **Bing URL Submission API**, within the daily quota Bing reports.
   - **Search Console sitemaps:** a sitemap is submitted only when it isn't registered or has gained URLs.
   - Only URLs that are indexable right now are sent: 200 status, no redirect, no `noindex`, self-canonical, allowed by robots.txt.
   - Each URL goes out once per channel, and again only when its `lastmod` changes.
6. **Triage and report.** Every URL gets one bucket and one concrete action. See `out/`.

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
