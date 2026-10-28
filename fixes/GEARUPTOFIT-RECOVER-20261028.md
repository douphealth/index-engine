# gearuptofit: recover-first cleanup (prepared 28 Oct 2026)

Claude's settings block it from writing to live sites, so this is yours to run. One script, two batches, a dry run first, a backup, a canary page, and a one-command rollback.

## What the data says (so you know what this is and is not)

- Bing clicks on gearuptofit fell gradually over about five weeks (from late August to now). Impressions fell in step and click-through stayed about the same. There is **no step on one date** and no page-level cause: the 19 pages that lost the most look like the pages that held steady (same size, same internal links, same October save), and the top queries kept their positions (1 to 4). The loss is spread over many one-off long-tail queries.
- So **rewriting the top losers is not supported by the evidence**, and the Revival page's generic hint ("improve it with first-hand, sourced detail") must not be followed by inventing first-hand detail. This script does not rewrite anything.
- Bing's own crawl issues for the site (642 URLs) are old product-post URLs: redirects, 404s, and five old server errors that now answer 404. None is linked from your pages and none is in a current content area, so they are not the cause.
- What **is** demonstrably wrong on pages that earn Bing clicks, and fixable: invented first-hand claims and invented expert quotes (against AGENTS.md), and 6 posts whose Article schema embeds whole base64 JPEGs (pages served at 1.1 to 1.8 MB instead of about 250 KB), plus one post with two broken inline images (1.1 MB of dead weight).

## What the script removes

Nothing is added and no other text changes. Run the dry run and read every cut.

- Batch 1 (8 pages, all earned Bing clicks): "We tested 150 miles", "We tested 15 models...", "We tested 18 multivitamins", "My clients see the best results", "In my practice, over 60% of new clients"; quotes attributed to Dr. Stacy Sims (2 posts), Dr. Jane Ellis, Dr. Elena Ruiz, Dr. Lena Cho (twice), Dr. Lena Torres and Dr. Sarah Booth; the invented bylines "Written by: Dr. Alex Rivera, Certified Primal Fitness Expert", "By John Doe, NASM-certified trainer" and "(Dr. Jane Smith, ACSM trainer)"; and the base64 images in the multivitamin post's schema.
- Batch 2 (7 pages): the quotes from Dr. Lena Torres (two posts, five quotes under different titles), Dr. Elena Torres, Dr. Lisa Chen, Dr. Layne Norton, Dr. Darren Candow, Dr. Sarah Chen, Dr. Maya Lopez and Dr. Nina Patel; the base64 images in six posts' schema; and the two broken inline images in the outdoor-running-etiquette post (decoded and saved next to the backup).

If you disagree with any cut, delete that item from `PAGES` in the script, or run with `--only <post id>`.

## How to run it

**One click:** double-click `D:\Claude\SEO-Setup\index-engine\fixes\run-gutf-recover-20261028.cmd`. It does everything below in the right order and stops on any error: dry run, pause, canary page, pause so you can look at it, the rest of batch 1, batch 2, then a read-only verify. After it finishes, tell Claude "verify" and it re-checks the live pages (`--verify`, read-only, no login).

Or step by step:

```
python "D:\Claude\SEO-Setup\index-engine\fixes\gutf_recover_20261028.py"
```
Dry run of both batches. Reads the live pages, writes nothing.

```
python "D:\Claude\SEO-Setup\index-engine\fixes\gutf_recover_20261028.py" --batch 1 --apply --canary-only
```
Writes the first (smallest) page only. Open it in a browser with `?nc=anything` after the address and look at it.

```
python "D:\Claude\SEO-Setup\index-engine\fixes\gutf_recover_20261028.py" --batch 1 --apply
```
Does the rest of batch 1 (the canary page is recognised as already done). Then the same with `--batch 2`.

Each `--apply` run: takes a verified JSON backup of every page in the batch first, runs `site_guard.py check`, saves each page and checks that WordPress stored exactly the intended text and that the page still answers 200, runs the guard again (a new BLOCK rolls the batch back by itself), and adds an entry to `CHANGELOG-AGENTS.md`.

Backups are in `01-gearuptofit\backups\2026-10-28\recover\`.

## Undo

```
python "D:\Claude\SEO-Setup\index-engine\fixes\gutf_recover_20261028.py" --rollback "<the backup-batchN-....json path the run printed>"
```
It will not overwrite a page that was edited after the script ran unless you add `--force`.

## After it runs

- Every save re-dates a post and its sitemap `lastmod` (AGENTS.md rule 2). That is why it is two small batches with a stated reason for each page.
- The edge can keep serving the old copy for up to about 4 hours. Purge Seraphinite (all cache + server cache), then Cloudflare, if you want it sooner. The script prints when each cached copy refreshes.
- Do **not** resubmit these URLs for indexing: they are already known, and pinging unchanged URLs is forbidden by AGENTS.md.
- If `site_guard.py check` alerts only on these pages and you accept that, run `python site_guard\site_guard.py snapshot`.
- Re-check in two to four weeks on the Revival page. This cleans up trust and page weight; it is not a promise of more clicks.

## Not touched (editorial calls, yours)

- Dr. Andy Galpin and Dr. Daniel Lieberman mentioned inline in the paleo post.
- "[1]" and "[2]" citation markers with no references list (smartwatch posts).
- "Published: October 1, 2025 | Updated: October 1, 2025" in the paleo post's author box.
- Invisible "Image generation failed" HTML comments in the paleo post.
- 24 other indexable gearuptofit pages that the Index Engine flags for invented claims but that earned no Bing clicks in the last 8 weeks, and the same family of problem (invented doctors, invented quotes) very likely exists on pages the engine's scanner cannot see: it only recognises a named doctor followed by a year.
- `/weight-loss/is-mediterranean-diet-the-healthiest-in-the-world/` (7.7 MB of stored content) and 6 other oversized posts are noindex (pruned), so they were left alone.
