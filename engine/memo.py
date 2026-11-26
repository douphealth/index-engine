# -*- coding: utf-8 -*-
"""Results that depend only on a post's stored content, kept between runs.

Three scans read every stored WordPress post on every analysis: the template scan (headings and pipeline leftovers),
the cleanup scan (sections and their text) and the link search (the words of every sentence). On 25 Nov 2026 that was
118 of the 145 seconds an analysis took, although a post's content rarely changes between two runs.

A result is stored next to a digest of the exact bytes it was computed from and of the version of the code that
computed it. It is reused only while both are unchanged, so a cached answer is always the answer a fresh computation
would give; anything else is recomputed and stored again. Deleting the table costs time on the next run, nothing else.
Storing a result commits the open transaction, so the work is kept even when a later step of the same run fails.
"""
import hashlib
import json
import zlib

SITE_KEY = "*"          # the row that holds a result for the whole site (it depends on every post of the site)


def digest(blob, version):
    """Digest of one stored post (its compressed bytes) under one version of the code that reads it."""
    return hashlib.blake2b(version.encode("utf-8") + b"\0" + bytes(blob), digest_size=12).hexdigest()


def _pack(value):
    return zlib.compress(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 3)


def _unpack(data):
    return json.loads(zlib.decompress(data).decode("utf-8"))


def _text(blob):
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except (zlib.error, TypeError):
        return None


def per_post(db, site, kind, version, compute):
    """{url: compute(post content)} for every stored post of a site, in the order the posts are stored. compute is
    called only for posts whose content (or the code version) changed since the last run. It must return plain JSON
    data (lists, dicts, strings, numbers), which is what a cached result is read back as."""
    have = {r[0]: (r[1], r[2]) for r in db.execute("SELECT url, digest, data FROM content_memo WHERE site=? AND kind=?",
                                                    (site, kind))}
    out, fresh = {}, []
    for url, blob in db.execute("SELECT url, raw FROM link_corpus WHERE site=?", (site,)).fetchall():
        if blob is None:
            continue
        d = digest(blob, version)
        hit = have.pop(url, None)
        if hit and hit[0] == d:
            try:
                out[url] = _unpack(hit[1])
                continue
            except (zlib.error, ValueError, TypeError):
                pass                                   # a damaged row: recompute it
        raw = _text(blob)
        if raw is None:
            continue
        data = _pack(compute(raw))
        out[url] = _unpack(data)                       # exactly what a later run will read back
        fresh.append((url, kind, site, d, data))
    if fresh:
        db.executemany("INSERT OR REPLACE INTO content_memo(url, kind, site, digest, data) VALUES(?,?,?,?,?)", fresh)
    gone = [(u, kind) for u in have if not u.startswith(SITE_KEY)]
    if gone:                                           # posts that were unpublished or moved
        db.executemany("DELETE FROM content_memo WHERE url=? AND kind=?", gone)
    if fresh or gone:
        db.commit()                                    # kept even if a later step of this run fails
    return out


def per_site(db, site, kind, version, compute):
    """compute({url: post content}) for a result that depends on all of a site's posts together, recomputed only when
    any post was added, removed or changed. Returns plain JSON data, or None when the site has no stored posts."""
    rows = db.execute("SELECT url, raw FROM link_corpus WHERE site=?", (site,)).fetchall()
    rows = [(u, b) for u, b in rows if b is not None]
    if not rows:
        return None
    h = hashlib.blake2b(version.encode("utf-8"), digest_size=12)
    for url, blob in sorted(rows, key=lambda r: r[0]):
        h.update(b"\0" + url.encode("utf-8") + b"\0" + hashlib.blake2b(bytes(blob), digest_size=12).digest())
    d = h.hexdigest()
    row = db.execute("SELECT digest, data FROM content_memo WHERE url=? AND kind=? AND site=?", (SITE_KEY + site, kind, site)).fetchone()
    if row and row[0] == d:
        try:
            return _unpack(row[1])
        except (zlib.error, ValueError, TypeError):
            pass
    posts = {}
    for url, blob in rows:
        raw = _text(blob)
        if raw is not None:
            posts[url] = raw
    data = _pack(compute(posts))
    db.execute("INSERT OR REPLACE INTO content_memo(url, kind, site, digest, data) VALUES(?,?,?,?,?)",
               (SITE_KEY + site, kind, site, d, data))
    db.commit()
    return _unpack(data)
