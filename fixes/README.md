# Sitemap/robots conflicts found 23 Oct 2026: what is left to run

Claude's permission settings blocked Claude from writing to the live sites, so these steps are yours. Each one is a single batch. Every script does a dry run unless you add `--apply`. It writes a JSON backup before changing anything, verifies the live pages afterwards, and prints its own rollback command.

| # | Site | What and why | How |
|---|---|---|---|
| 1 | mysticaldigits | Posts 1450, 6549 and 7397 are noindexed on purpose (25 Sep triage, 0 Bing clicks) but are still in Yoast's post sitemap. The fix sets Yoast's own noindex flag so Yoast drops them from the sitemap; the served robots stays noindex. | `python D:\Claude\SEO-Setup\index-engine\fixes\fix_sitemap_conflicts_20261023.py --site mysticaldigits.com --apply` |
| 2 | frenchyfab | Post 117, `dental-care-tips-for-french-bulldogs`, is published but 301s to `/french-bulldog-dental-care/` (last Bing click May 2026). The fix moves the post to draft and keeps the redirect; the script republishes the post if the redirect stops working. `/tag/featured-post/` and `/author/alexios-papaioannou-2/` are blocked by robots.txt but listed in Yoast's sitemaps; the fix sets Yoast noindex on that term and that author. | `python D:\Claude\SEO-Setup\index-engine\fixes\fix_sitemap_conflicts_20261023.py --site frenchyfab.com --apply` |
| 3 | efficientgptprompts | `/shop/` (47 words, 0 clicks) is noindexed but listed in `product-sitemap.xml`. Code Snippets there will not run the script's temporary route, so set the flag by hand. | wp-admin > Pages > Shop > Yoast box > Advanced > "Allow search engines to show this Page in search results?" = **No** > Update |
| 4 | gearuptofit | The Adizero Boston 13 and Dropset Pro Hybrid reviews are missing from `/sitemap-posts.xml`, because `origin.gearuptofit.com` serves a stale copy of Yoast's sitemap. This is a cache purge, with no code change. | Seraphinite > Operate > Specified URLs > delete the cache of `https://gearuptofit.com/post-sitemap.xml` and `https://gearuptofit.com/post-sitemap2.xml`. After 1 hour, `/sitemap-posts.xml` should list 583 URLs. |
| 5 | micegoneguide | **Approved 23 Oct.** Move post 928 (`/rodent-proofing-for-new-constructions/`) to draft and 301 it to `/mouse-proofing/` (page 3160). The redirect is a new snippet copying #154's pattern, in the site's existing redirect layer. It is proven live before the post changes, so the URL is never a 404. The script then purges Cloudflare, verifies, notifies Bing and logs to `05-micegoneguide/CHANGELOG-2026-10.md`. The four pages that linked to the post (related-posts blocks and previous/next links) drop the link by themselves. | `python D:\Claude\SEO-Setup\index-engine\fixes\mgg_consolidate_rodent_proofing_20261023.py --apply` (dry run first without `--apply`; `--rollback <backup.json>` undoes it) |

After each batch, the next daily Index Engine run (10:30) re-checks every URL and the report shows whether the conflict has cleared. To verify one batch at once, run:

```
python D:\Claude\SEO-Setup\index-engine\indexengine.py run --site <site> --force-audit --no-submit --inspect-max 0
```
