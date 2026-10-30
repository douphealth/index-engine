# Indexation Workspace

## What the workspace does

Open `/workspace` after signing in, or choose **Issues workspace** from the existing dashboard navigation.

The workspace reads the same protected portfolio snapshots, URL state and run queue as the existing dashboard. It does not add another copy of credentials or a second analytics database.

1. Review availability problems and historically valuable broken URLs first.
2. Check whether noindex, redirects, robots exclusions and alternate canonicals are intentional. They are not automatic deletion or rewrite instructions.
3. Use the existing link approval and traffic-recovery screens for interventions.
4. Review inspection evidence collected after the intervention. Recording a manual Google request does not send one, and an indexed observation does not prove causation.

The workspace supports search, website/type/workflow filters, notes saved in D1, reversible ignore decisions, a measure-only website re-audit, the existing Connection Doctor queue, and CSV export of filtered evidence. CSV cells starting with formula characters are neutralized.

## Evidence and compatibility

The Python publisher now appends original audit, HTTP, canonical, Google inspection, source-click timestamps and the latest applied-link date. Original columns remain in their original positions. A new upload time does not overwrite the observation dates.

Old snapshots still render. Missing timestamps and unavailable source data are shown as missing, not as a current successful check or zero clicks. A failed website load is excluded from loaded counts and named in coverage; it is not counted as healthy.

Actions are recorded separately from outcome evidence. Follow-up can be untracked, awaiting later inspection, stale, inspected without confirmation, or observed indexed in a later successful inspection. There is no automatic `resolved` claim from a button click or a Bing/IndexNow submission.

## Safe local-engine rollout

Cloudflare deployment updates the dashboard, not a separate Windows checkout. To supply the added evidence columns, the actual engine host must use the matching code.

Before updating, let the current engine run finish, retain the existing SQLite backup and credential files outside Git, and inspect `git status --short`. Do not reset or overwrite uncommitted local changes.

From a clean local engine checkout:

```text
git pull --ff-only
python -m unittest discover -s tests -v
python indexengine.py selftest
python indexengine.py publish
```

`publish` uses the existing command's no-submission/no-link-change behavior to upload the current local observations. It does not make those observations fresh. Use **Re-audit website** for a measure-only recrawl; Google inspection remains subject to the engine's eligibility, scheduling and quota rules. Check **Runs** for completion and **Connections** for the actual diagnostic report.

Do not put GSC JSON, Bing API keys, WordPress passwords or Worker bearer tokens into this repository or into the page scripts. Do not rotate or replace a production credential without updating all legitimate clients that depend on it.

## Validation

- Python unit tests and the existing offline engine selftest.
- Node decision tests and queue tests against SQLite.
- Worker build and script syntax checks.
- Chromium workflow tests against synthetic fixtures, including production-style CSP, 360/768/1440px layouts, note-save failure recovery, reversible ignore, safe command options, partial source failures, inert untrusted titles and expired sessions.

Browser screenshots are named `*-fixture.png` and are not production analytics. Run the browser suite with:

```text
python tests/qa_workspace_browser.py
```

`--offline-content` is only a fallback for restricted local navigation. It does not replace the normal CI browser job.

The deployment workflow calls the full validation workflow and requires its success before database migrations and Worker deployment. The release adds no database migrations. Authenticated production access, the actual Windows scheduler and third-party property authorization must still be verified in their real environment.

## Rollback

Revert this pull request and let the normal validated deployment complete. The original dashboard screens are retained and the new publisher columns are additive, so old clients continue to use the original fields. No D1 schema reversal or content restoration is required by this release. Do not reset databases or remove owner decisions to roll back the interface.

## Boundaries

This release is not an always-on cloud replacement for the Python engine. It does not bulk-edit WordPress, bypass Google indexing rules, guarantee crawl/index inclusion, or automatically execute site-specific recovery scripts. Its purpose is to make the actual evidence, safe next action and outstanding verification visible in one place.
