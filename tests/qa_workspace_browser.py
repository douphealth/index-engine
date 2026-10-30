"""Credential-free browser regression: synthetic fixtures only, never live APIs.
Run: python tests/qa_workspace_browser.py [--browser /usr/bin/chromium]
Requires Playwright; output screenshots are explicitly test fixtures.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'dashboard' / 'public'
OUTPUT = ROOT / 'qa-output'


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--browser')
    args.add_argument('--offline-content', action='store_true')
    opts = args.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat()
    cols = ['url', 'bucket', 'title', 'flags', 'checked_at', 'inspected_at', 'google_verdict', 'gsc_clicks', 'gsc_at', 'http_status', 'reason', 'action']
    def row(slug, bucket, title=None, clicks=0):
        data = dict(url='https://one.example/' + slug, bucket=bucket, title=title or slug, flags='', checked_at=stamp,
                    inspected_at=stamp, google_verdict='PASS' if bucket == 'INDEXED' else 'NEUTRAL', gsc_clicks=clicks,
                    gsc_at=stamp, http_status=404 if bucket == 'BROKEN' else 200,
                    reason='Synthetic test fixture; not production analytics.', action='Review the recorded evidence before changing this page.')
        return [data.get(k, '') for k in cols]
    rows = [row('recover', 'BROKEN', 'Recover this historically valuable page', 150), row('private', 'NOINDEX', 'Review an intentional exclusion'),
            row('safe', 'INDEXED', 'Healthy page'), row('unsafe-title', 'CRAWLED_NOT_INDEXED', '<img src=x onerror=window.injected=1>')]
    rows += [row('guide-' + str(i), 'CRAWLED_NOT_INDEXED', 'Useful guide ' + str(i)) for i in range(45)]
    summary = {'generated_at': stamp, 'sites': [{'site': 'one.example', 'gsc_property': 'sc-domain:one.example'}, {'site': 'two.example'}]}
    owner = {'settings': {'engine_last_seen': {'value': {'at': stamp}}, 'sites_disabled': {'value': []}}, 'url_state': [], 'commands': []}
    tables = {'one.example': {'site': 'one.example', 'generated_at': stamp, 'cols': cols, 'rows': rows},
              'two.example': {'site': 'two.example', 'generated_at': stamp, 'cols': cols, 'rows': []}}
    switches = {'note_failure': False, 'partial': False, 'unauthorized': False}
    posts, errors = [], []
    OUTPUT.mkdir(exist_ok=True)
    checks = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=opts.browser or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        def route(r):
            path = urlsplit(r.request.url).path
            def reply(data, code=200):
                r.fulfill(status=code, content_type='application/json', body=json.dumps(data))
            if switches['unauthorized']:
                return reply({'error': 'unauthorized'}, 401)
            if r.request.method == 'POST':
                body = r.request.post_data_json
                posts.append((path, body))
                if path == '/api/command':
                    return reply({'ok': True, 'id': 11, 'status': 'pending'})
                if path == '/api/url':
                    if body['action'] == 'note' and switches['note_failure']:
                        return reply({'error': 'Synthetic save failure'}, 500)
                    current = next((x for x in owner['url_state'] if x['url'] == body['url']), {'url': body['url'], 'site': body['site']})
                    if body['action'] == 'note':
                        current['note'] = body['note']
                    elif body['action'] in ('ignore', 'unignore'):
                        current['ignored'] = int(body['action'] == 'ignore')
                    elif body['action'] == 'requested':
                        current['requested_at'] = stamp
                    elif body['action'] == 'unrequested':
                        current['requested_at'] = None
                    owner['url_state'] = [x for x in owner['url_state'] if x['url'] != body['url']] + [current]
                    return reply({'ok': True, 'url_state': current})
                return reply({'error': 'Unexpected write'}, 400)
            if path == '/api/data/summary': return reply(summary)
            if path == '/api/state': return reply(owner)
            if path == '/api/data/health': return reply({'checked_at': stamp, 'sources': {}})
            if path.startswith('/api/data/site/'):
                site = path.rsplit('/', 1)[1]
                if switches['partial'] and site == 'two.example': return reply({'error': 'no data yet'}, 404)
                return reply(tables[site])
            return reply({'error': 'Unexpected API read'}, 400)
        def all_routes(r):
            path = urlsplit(r.request.url).path
            if path.startswith('/api/'):
                return route(r)
            if path == '/login':
                return r.fulfill(content_type='text/html', body='<html><body>Test login required</body></html>')
            target = PUBLIC / ('workspace.html' if path == '/workspace' else path.lstrip('/'))
            if target.parent != PUBLIC or not target.is_file():
                return r.fulfill(status=404, body='Not found')
            content_type = 'text/html' if target.suffix == '.html' else 'text/css' if target.suffix == '.css' else 'application/javascript'
            r.fulfill(content_type=content_type, body=target.read_text(), headers={'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        page.route('**/*', all_routes)
        if opts.offline_content:
            import re
            html = (PUBLIC / 'workspace.html').read_text()
            html = re.sub(r'<script[^>]*></script>|<link[^>]*>', '', html)
            page.set_content(html)
            page.add_style_tag(content=(PUBLIC / 'workspace.css').read_text())
            def offline_fetch(path, data):
                class Req:
                    url = 'https://workspace.test' + path
                    method = 'POST' if data else 'GET'
                    post_data_json = json.loads(data) if data else None
                class FakeRoute:
                    request = Req()
                    result = None
                    def fulfill(self, status=200, content_type=None, body=''):
                        self.result = {'status': status, 'body': body}
                fake = FakeRoute()
                route(fake)
                return fake.result
            page.expose_function('fixtureFetch', offline_fetch)
            page.add_script_tag(content="window.fetch=async (path, options={}) => {const r=await window.fixtureFetch(path, options.body || null);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});};")
            page.add_script_tag(content=(PUBLIC / 'workspace-model.js').read_text())
            page.add_script_tag(content=(PUBLIC / 'workspace.js').read_text())
        else:
            page.goto('https://workspace.test/workspace')
        expect(page.locator('#coverage')).to_contain_text('2 / 2 sites loaded')
        expect(page.locator('#detail')).to_contain_text('Recover this historically valuable page')
        assert not errors, errors
        assert '\nnull\n' not in page.locator('body').inner_text()
        checks.append('complete initial render' + (' (offline DOM, CSP tested in CI)' if opts.offline_content else ' with production CSP'))
        page.screenshot(path=str(OUTPUT / 'workspace-desktop-fixture.png'), full_page=True)
        for width in [360, 768, 1440]:
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2'), str(width)
            if width == 360:
                page.screenshot(path=str(OUTPUT / 'workspace-mobile-fixture.png'), full_page=True)
        checks.append('360px / 768px / 1440px without page overflow')
        page.get_by_role('button', name='Next', exact=True).click()
        expect(page.locator('#page')).to_have_text('2 / 2')
        page.get_by_role('button', name='Previous', exact=True).click()
        checks.append('paginated issue queue')
        page.locator('#search').fill('recover')
        expect(page.locator('#count')).to_have_text('1 URLs')
        page.get_by_role('button', name='Re-audit website', exact=True).click()
        expect(page.locator('#notice')).to_contain_text('Measure-only audit queued')
        assert posts[-1][1]['options'] == {'site': 'one.example', 'no_submit': True, 'force_audit': True}
        checks.append('measure-only action sends exact safe options')
        page.locator('#url-note').fill('Preserve this note on failed save')
        switches['note_failure'] = True
        page.get_by_role('button', name='Save note', exact=True).click()
        expect(page.locator('#notice')).to_contain_text('Synthetic save failure')
        expect(page.locator('#url-note')).to_have_value('Preserve this note on failed save')
        expect(page.get_by_role('button', name='Save note', exact=True)).to_be_enabled()
        switches['note_failure'] = False
        page.get_by_role('button', name='Save note', exact=True).click()
        expect(page.locator('#notice')).to_have_text('Note saved.')
        checks.append('failed-save recovery preserves edits and re-enables retry')
        page.get_by_role('button', name='Record manual request', exact=True).click()
        expect(page.locator('#detail')).to_contain_text('Awaiting a later Google inspection')
        checks.append('manual request never becomes verified indexed')
        page.get_by_role('button', name='Ignore this URL', exact=True).click()
        expect(page.locator('#count')).to_have_text('0 URLs')
        page.locator('#ignored').check()
        expect(page.locator('#count')).to_have_text('1 URLs')
        page.get_by_role('button', name='Stop ignoring', exact=True).click()
        expect(page.locator('#detail')).to_contain_text('Ignore this URL')
        checks.append('reversible persistent ignore decision')
        page.locator('#search').fill('unsafe-title')
        expect(page.locator('#count')).to_have_text('1 URLs')
        assert page.evaluate('window.injected === undefined')
        checks.append('untrusted title remains inert text')
        page.locator('#search').fill('')
        switches['partial'] = True
        page.get_by_role('button', name='Refresh evidence', exact=True).click()
        expect(page.locator('#notice')).to_contain_text('Partial evidence only')
        expect(page.locator('#coverage')).to_contain_text('1 / 2 sites loaded')
        checks.append('partial coverage remains explicit')
        if not opts.offline_content:
            switches['unauthorized'] = True
            page.get_by_role('button', name='Refresh evidence', exact=True).click()
            page.wait_for_url('**/login')
            checks.append('expired session returns to login')
        assert not errors, errors
        browser.close()
    print(json.dumps({'passed': len(checks), 'checks': checks, 'scope': 'Synthetic fixtures, not authenticated production data'}, indent=2))


if __name__ == '__main__':
    main()
