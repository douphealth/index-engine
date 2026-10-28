# -*- coding: utf-8 -*-
"""gearuptofit.com: cut invented first-hand claims and invented expert quotes, and remove 1-2 MB of embedded image data, from pages
that earn Bing clicks or carry a verified page-weight defect.

Prepared by Claude on 28 Oct 2026 from the Index Engine's Revival page. Owner-run: Claude's settings block it from writing to live sites.

    python gutf_recover_20261028.py                          # dry run, both batches: reads the live pages, shows every cut, writes nothing
    python gutf_recover_20261028.py --batch 1                # dry run of one batch
    python gutf_recover_20261028.py --batch 1 --apply        # backup, canary page, verify, the rest, guard, changelog
    python gutf_recover_20261028.py --batch 1 --apply --canary-only     # stop after the first (smallest) page
    python gutf_recover_20261028.py --batch 1 --apply --only 85478 63825   # just these post ids
    python gutf_recover_20261028.py --rollback <backup.json> # put the pages back exactly as they were
    python gutf_recover_20261028.py --verify                 # read-only: are the live pages clean (?nc= copy and what visitors get)

What it removes (AGENTS.md: "No invented testing, experience, ratings, reviews, statistics, quotes or credentials"). Nothing is added and no
other text changes. Read the dry run: every cut is printed in full.
  * first-person claims the site cannot back: "We tested 150 miles", "We tested 18 multivitamins", "My clients see the best results",
    "In my practice, over 60% of new clients"
  * quotes attributed to named doctors and researchers with a job title, institution or year that appears nowhere else on the site (the same
    "Dr. Lena Torres" is quoted six times across three posts under five different job titles or affiliations); real people (Stacy Sims, Layne Norton, Darren Candow) are quoted with
    words nobody has traced to a source
  * three invented bylines: "Written by: Dr. Alex Rivera, Certified Primal Fitness Expert", "By John Doe, NASM-certified trainer", and the
    "(Dr. Jane Smith, ACSM trainer)" aside
  * 6 posts whose Article JSON-LD embeds whole base64 JPEGs as ImageObject.url (0.4 to 0.9 MB each; the pages are served at 1.1 to 1.8 MB
    instead of ~250 KB, and a data: URI is not a valid structured-data image URL): the data: image entries are dropped from the JSON-LD
  * 1 post (outdoor running etiquette) with two <img src="image/jpeg;base64,..."> tags that have no "data:" prefix, so browsers cannot display
    them (1.1 MB of dead weight): the two figures are removed, and the decoded JPEGs are saved next to the backup in case you want them back

Second wave (batches 3 to 5, prepared 28 Oct): 18 more indexable pages the trust scan flags. Same rules and the same dry run. Pages whose
invented material runs through the whole text (two review/story pages, one invented experiment, one invented tasting panel) are NOT cut here:
they need a rewrite. See the guide.

Left for you (not edited, because they are editorial calls): Dr. Andy Galpin / Dr. Daniel Lieberman mentioned inline in the paleo post;
"[1]" / "[2]" citation markers with no references list; "Published: ... Updated: ..." dates in the paleo author box; invisible
"Image generation failed" HTML comments in the paleo post; whatever else on these pages is unsourced. These pages need a proper editorial
pass with named sources; this script only removes what is demonstrably invented.

Safety (AGENTS.md "Safe change protocol"): dry run by default; at most 8 pages per batch; every page backed up (REST context=edit raw) to one
JSON file before the first write; one canary page is written and verified (REST read-back must equal the intended text exactly, and the served
page, fetched with ?nc=, must answer 200) before the others; site_guard.py check runs before and after, and a NEW BLOCK rolls the batch back
automatically; a page whose text no longer matches what this script expects is skipped, never guessed at; an entry is added to
CHANGELOG-AGENTS.md. Every save re-dates a post and its sitemap lastmod (AGENTS.md rule 2): that is why the scope is two small batches,
each page with a stated reason. Credentials are never printed.

After --apply, the edge can serve the old copy for up to ~4 h (AGENTS.md "How the site is built"): purge Seraphinite (all cache + server
cache), then Cloudflare, if you want it sooner. Do NOT resubmit these URLs for indexing; they are already known, and unchanged-URL pings are
forbidden (AGENTS.md "Re-indexing safely" #5).
"""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from engine import audit, config, wp  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SITE = "gearuptofit.com"
BASE = "https://gearuptofit.com"
GUARD = Path(config.SITE_GATES[SITE])
SITE_DIR = config.PORTFOLIO / "01-gearuptofit"
CHANGELOG = SITE_DIR / "CHANGELOG-AGENTS.md"
BACKUP_DIR = SITE_DIR / "backups" / "2026-10-28" / "recover"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}
MAX_PER_BATCH = 8

# edits: ("cut", exact text) | ("quote", name in the attribution, how many quotes name them) | ("schema",) | ("figures", expected count)
# gone: phrases that must not be on the served page afterwards (checked on the ?nc= copy; a miss only means "wait for the cache")
SCHEMA = ("schema",)
PAGES = [
    dict(batch=1, id=85478, path="/review/asics-sonicblast/", why="'We tested 150 miles.' (the site has no test data on file)",
         edits=[("cut", " We tested 150 miles. Fresh insights here.")], gone=["We tested 150 miles"]),
    dict(batch=1, id=63825, path="/review/best-smartwatches-for-weightlifters/",
         why="'We tested 15 models across 60+ lifting sessions' and a quote from 'Dr. Elena Ruiz, Lead Biomechanics Researcher, SportsTech Labs'",
         edits=[("cut", " We tested 15 models across 60+ lifting sessions to find the real winners for powerlifters, bodybuilders, "
                        "and daily gym warriors."), ("quote", "Dr. Elena Ruiz", 1)], gone=["We tested 15 models", "Elena Ruiz"]),
    dict(batch=1, id=67441, path="/fitness/strength-training-with-dumbbells-at-home-for-women-over-60/", why="'My clients see the best results'",
         edits=[("cut", "<p>My clients see the best results with these 2026 strategies:</p>\n")], gone=["My clients see the best results"]),
    dict(batch=1, id=39201, path="/fitness/paleotraining-an-effective-training-method/",
         why="'In my practice, over 60% of new clients...' and the byline 'Written by: Dr. Alex Rivera, Certified Primal Fitness Expert'",
         edits=[("cut", "In my practice, <strong>over 60%</strong> of new clients come in with overuse injuries from doing too much, too soon. "),
                ("cut", '<p style="margin:5px 0;"><strong>Written by:</strong> Dr. Alex Rivera, Certified Primal Fitness Expert with 15+ Years '
                        'in Evolutionary Training</p>\n')], gone=["In my practice", "Alex Rivera"]),
    dict(batch=1, id=52613, path="/running/running-vs-walking-calories/",
         why="quote from 'Dr. Jane Ellis, 2025 ACSM Report', '(Dr. Jane Smith, ACSM trainer)' and the placeholder byline 'By John Doe, NASM-certified trainer'",
         edits=[("quote", "Dr. Jane Ellis", 1), ("cut", " (Dr. Jane Smith, ACSM trainer)"),
                ("cut", "<li>By John Doe, NASM-certified trainer, 15+ years, featured in Runner&#8217;s World.</li>\n")],
         gone=["Jane Ellis", "Jane Smith", "John Doe"]),
    dict(batch=1, id=48672, path="/nutrition/does-beer-ruin-your-workout/", why="quote attributed to 'Dr. Stacy Sims, 2025 Women's Performance Summit'",
         edits=[("quote", "Dr. Stacy Sims", 1)], gone=["Dr. Stacy Sims"]),
    dict(batch=1, id=39617, path="/fitness/how-to-train-after-the-age-of-40/", why="quote attributed to 'Dr. Stacy Sims, 2025 Master's Athlete Conference'",
         edits=[("quote", "Dr. Stacy Sims", 1)], gone=["Dr. Stacy Sims"]),
    dict(batch=1, id=47852, path="/health/supplements/ranking-the-best-multivitamin-for-women-over-50/",
         why="'We tested 18 multivitamins', 4 invented expert quotes (Dr. Lena Cho twice, Dr. Lena Torres, Dr. Sarah Booth), 1.5 MB of base64 images in the schema",
         edits=[("cut", " We tested 18 multivitamins in 2025. Six meet all criteria."), ("quote", "Dr. Lena Cho", 2), ("quote", "Dr. Lena Torres", 1),
                ("quote", "Dr. Sarah Booth", 1), SCHEMA],
         gone=["We tested 18 multivitamins", "Lena Cho", "Lena Torres", "Sarah Booth"], schema=True),
    dict(batch=2, id=70614, path="/fitness/crossfit-vs-bootcamp/",
         why="quote attributed to 'Dr. Nina Patel, J-Strength 2025'; quote 'Our clients' body-fat dropped 3 percent...' attributed to '2025 CrossFit Mayhem Programming Notes'; "
             "base64 images in the schema",
         edits=[("quote", "Dr. Nina Patel", 1), ("quote", "Mayhem Programming Notes", 1), SCHEMA], gone=["Nina Patel", "Mayhem Programming Notes"], schema=True),
    dict(batch=2, id=47373, path="/health/supplements/ranking-the-best-fish-oil-supplements/",
         why="quotes from 'Dr. Sarah Chen, Cardiologist, Cleveland Clinic' and 'Dr. Maya Lopez, University of Miami, 2024'; base64 images in the schema",
         edits=[("quote", "Dr. Sarah Chen", 1), ("quote", "Dr. Maya Lopez", 1), SCHEMA], gone=["Sarah Chen", "Maya Lopez"], schema=True),
    dict(batch=2, id=47354, path="/health/supplements/ranking-the-best-creatine-for-men/",
         why="quote attributed to 'Dr. Darren Candow, 2025 meta-analysis'; base64 images in the schema",
         edits=[("quote", "Dr. Darren Candow", 1), SCHEMA], gone=["Darren Candow"], schema=True),
    dict(batch=2, id=47403, path="/health/supplements/ranking-the-best-protein-powder-for-weight-loss-female/",
         why="3 quotes from 'Dr. Lena Torres' under 3 different titles; base64 images in the schema",
         edits=[("quote", "Dr. Lena Torres", 3), SCHEMA], gone=["Lena Torres"], schema=True),
    dict(batch=2, id=47386, path="/health/supplements/ranking-the-best-supplements-that-gain-weight/",
         why="quotes from 'Dr. Lena Torres' (twice, two titles) and 'Dr. Layne Norton, Physique Coach & Physician'; base64 images in the schema",
         edits=[("quote", "Dr. Lena Torres", 2), ("quote", "Dr. Layne Norton", 1), SCHEMA], gone=["Lena Torres", "Layne Norton"], schema=True),
    dict(batch=2, id=47875, path="/health/supplements/ranking-the-best-supplements-to-improve-kidney-function/",
         why="quotes from 'Dr. Elena Torres, nephrologist at Mayo Clinic (2025)' (twice) and 'Dr. Lisa Chen, Johns Hopkins'; base64 images in the schema",
         edits=[("quote", "Dr. Elena Torres", 2), ("quote", "Dr. Lisa Chen", 1), SCHEMA], gone=["Elena Torres", "Lisa Chen"], schema=True),
    dict(batch=2, id=77265, path="/running/outdoor-running-etiquette/", why="two <img> tags with src 'image/jpeg;base64,...' (no data: prefix): broken, 1.1 MB",
         edits=[("figures", 2)], gone=[]),
    # ---- second wave (28 Oct): the other indexable pages the trust scan flags. Pages that cannot be fixed by cutting are NOT here:
    # garmin-forerunner-55-review and bmi-and-body-composition (invented first-person stories throughout) and the walking-to-reduce-belly-fat
    # experiment need a rewrite; heart-rate-zone-calculator cites real researchers; best-running-shoes-for-plantar-fasciitis is no longer flagged.
    dict(batch=3, id=3189, path="/running/risks-of-running-as-a-beginner/",
         why="quote attributed to 'Dr. Lee Saxby, 2026 biomechanics report'",
         edits=[("quote", "Dr. Lee Saxby", 1)],
         gone=["Lee Saxby"]),
    dict(batch=3, id=68133, path="/fitness/why-back-and-biceps-workouts-are-effective/",
         why="3 quotes attributed to 'Dr. Layne Norton' (twice) and 'Dr. Eric Helms' at conferences/podcasts nobody can source",
         edits=[("quote", "Dr. Layne Norton", 2), ("quote", "Dr. Eric Helms", 1)],
         gone=["Muscle Science Review", "ISSN Conference Keynote", "Eric Helms"]),
    dict(batch=3, id=79446, path="/running/master-proper-running-form/",
         why="'I tell my clients to imagine...' recast as a plain instruction",
         edits=[("sub", "I tell my clients to imagine a laser beam from the crown of their head through their spine, exiting between their feet.", "Imagine a laser beam from the crown of your head through your spine, exiting between your feet.")],
         gone=["I tell my clients"]),
    dict(batch=3, id=78770, path="/fitness/advanced-fitness-techniques/",
         why="'I program this for 100% of my clients... a 31% reduction in reported muscle soreness'",
         edits=[("cut", " I program this for 100% of my clients on off-days. The result? A 31% reduction in reported muscle soreness.")],
         gone=["my clients on off-days"]),
    dict(batch=3, id=73650, path="/nutrition/what-to-eat-before-a-half-marathon/",
         why="'I tell my clients: ...'",
         edits=[("cut", " I tell my clients: &#8220;You&#8217;re not just eating food; you&#8217;re loading biochemical energy pellets.&#8221;")],
         gone=["I tell my clients"]),
    dict(batch=3, id=60149, path="/fitness/roller-skating-vs-running-which-is-better-for-your-health/",
         why="quotes attributed to 'Dr. Lina Lecoq, Oslo Sports Lab, 2025' and 'Dr. Lena Ortiz, 2025 ACSM Conference' (the second says 'For my clients over 40')",
         edits=[("quote", "Dr. Lina Lecoq", 1), ("quote", "Dr. Lena Ortiz", 1)],
         gone=["Lina Lecoq", "Lena Ortiz", "For my clients over 40"]),
    dict(batch=3, id=45805, path="/fitness/exercising-easier-when-you-feel-overwhelmed/",
         why="'Some of my clients never progress past Week 2'",
         edits=[("cut", " Some of my clients never progress past Week 2, and that&#8217;s perfectly fine.")],
         gone=["Some of my clients"]),
    dict(batch=3, id=71748, path="/fitness/beginners-guide-to-pilates/",
         why="'I tell my clients: ... 80% more likely' and 'my clients' progress accelerate by 300%'",
         edits=[("cut", " I tell my clients: &#8220;If your mat is rolled out, you&#8217;re 80% more likely to practice.&#8221;"), ("cut", " I was skeptical until I saw my clients&#8217; progress accelerate by 300% with guided correction.")],
         gone=["I tell my clients", "my clients"]),
    dict(batch=4, id=71670, path="/health/what-is-your-metabolic-age/",
         why="'In my practice, this awareness is the #1 motivator for clients'",
         edits=[("cut", " In my practice, this awareness is the #1 motivator for clients to commit to lasting change.")],
         gone=["In my practice"]),
    dict(batch=4, id=64298, path="/fitness/improve-posture-with-hiit-workouts/",
         why="quote attributed to 'Dr. Lena Ortiz, 2025 ACSM Annual Meeting Keynote'",
         edits=[("quote", "Dr. Lena Ortiz", 1)],
         gone=["Lena Ortiz"]),
    dict(batch=4, id=61777, path="/fitness/why-is-it-important-to-assess-flexibility/",
         why="quote attributed to 'Dr. Lisa Chow, sports physiotherapist' ('our 2025 athlete cohort') and 'protocols... I see in my clinic'",
         edits=[("quote", "Dr. Lisa Chow", 1), ("cut", " Here are protocols based on the most common restrictions I see in my clinic.")],
         gone=["Lisa Chow", "in my clinic"]),
    dict(batch=4, id=56726, path="/running/the-best-running-sneakers-for-achilles-tendonitis/",
         why="quote attributed to 'Dr. Kristin Lewis, DPM, 2025 Clinical Guidelines' (only the quote and its cite line: the product card after it stays) and 'in my lab'",
         edits=[("cut", "<p style=\"font-size: 1.3rem; font-weight: 700; color: #6d28d9; margin: 0 0 0.5rem 0; font-style: italic;\">\u201cControl four variables\u2014insole prescription, heel drop, weekly load changes, and surface polarity\u2014and you\u2019ve already halved recurrence rates regardless of age or mileage.\u201d</p>\n<p><cite style=\"font-size: 0.9rem; color: #7c3aed; font-style: normal; font-weight: 600;\">\u2014 Dr. Kristin Lewis, DPM, Foot &amp; Ankle Australia, 2025 Clinical Guidelines</cite>\n</p>\n"), ("sub", "2026 shoes that routinely pass this test in my lab:", "2026 shoes that routinely pass this test:")],
         gone=["Kristin Lewis", "in my lab"]),
    dict(batch=4, id=55368, path="/health/how-long-does-it-take-for-your-stomach-to-shrink/",
         why="quote attributed to 'Dr. Aisha Malik, 2025 Journal of Gastrointestinal Motility (Vol. 31, Issue 4)'",
         edits=[("quote", "Dr. Aisha Malik", 1)],
         gone=["Aisha Malik"]),
    dict(batch=4, id=54377, path="/weight-loss/eating-right-for-an-endomorphic-body-type/",
         why="'I cycle my clients through three phases' recast as a plain instruction",
         edits=[("sub", "I cycle my clients through three phases every 10\u201314 days:", "Cycle through three phases every 10\u201314 days:")],
         gone=["I cycle my clients"]),
    dict(batch=4, id=71002, path="/health/how-to-speed-up-metabolism-after-60/",
         why="'I'm a data geek, so I test everything' and 'gives my clients a' recast as 'gives you a'",
         edits=[("cut", "I\u2019m a data geek, so I test everything. "), ("sub", " gives my clients a ", " gives you a ")],
         gone=["data geek", "gives my clients"]),
    dict(batch=4, id=53249, path="/fitness/walking-to-reduce-belly-fat/",
         why="quote attributed to 'Dr. Kenji Ohmura, Stanford Metabolic Lab, 2025' (the invented first-person walking experiment on this page needs a rewrite and is NOT touched)",
         edits=[("quote", "Dr. Kenji Ohmura", 1)],
         gone=["Kenji Ohmura"], check_trust=False),
    dict(batch=5, id=46762, path="/nutrition/good-vs-bad-calories/",
         why="quotes attributed to 'Dr. Sarah Chen, MD' and a first-person 'In my practice, I've observed...' quote",
         edits=[("quote", "Dr. Sarah Chen", 1), ("quote", "In my practice, I&#8217;ve observed", 1)],
         gone=["Sarah Chen", "In my practice, I"]),
    dict(batch=5, id=21774, path="/nutrition/best-almond-butter/",
         why="two quotes attributed to 'Dr. Lena Torres' under two different titles, and 'We tested 12 / 27 brands' (the invented tasting panel elsewhere on this page needs a rewrite and is NOT touched)",
         edits=[("cut", "We tested 12 butter brands in 2025. Only 5 met strict organic standards. "), ("cut", "<p>Only three organic almond butter brands passed our 2025 testing standards. They met strict criteria: USDA-certified organic, no added oils, low sodium, and 75% fewer additives than industry average. Taste, texture, and nutrition matter. We tested 27 brands. Three won. All backed by clinical data showing improved satiety and lipid profiles [1].</p>\n"), ("quote", "Dr. Lena Torres", 2)],
         gone=["Lena Torres", "We tested 12"]),
    # ---- follow-up (28 Oct): the same invented testing claim was also left in the page's FAQ structured data. Its whole answer was the
    # paragraph batch 5 cut from the visible page, so the question is dropped from the schema rather than left with a claim the page no longer makes.
    dict(batch=6, id=21774, path="/nutrition/best-almond-butter/",
         why="FAQ schema answer 'Only three organic almond butter brands passed our 2025 testing standards... We tested 27 brands. Three won...' "
             "(the same text batch 5 cut from the visible page)",
         edits=[("faq_drop", "We tested 27 brands")], gone=["We tested 27"], check_trust=False),
]


class Drift(Exception):
    """The live text is not what this script was written against. The page is skipped."""


# ---------------------------------------------------------------- pure text edits (tested offline)

def cut_text(raw, text):
    if raw.count(text) != 1:
        raise Drift(f"expected exactly 1 copy of {text[:60]!r}, found {raw.count(text)}")
    return raw.replace(text, "", 1), [text]


def sub_text(raw, old, new):
    """Replace one exact phrase (a first-person claim recast as a plain instruction). Repeatable once done."""
    if raw.count(old) == 1:
        return raw.replace(old, new, 1), [f"{old}  =>  {new}"]
    if raw.count(old) == 0 and raw.count(new) >= 1:
        return raw, [f"already done: {short(new, 60)}"]
    raise Drift(f"expected exactly 1 copy of {old[:60]!r}, found {raw.count(old)}")


def cut_quote(raw, who, n=1):
    """Remove the <blockquote>s (with their wp:quote wrapper) whose text names `who`. Exactly n must exist."""
    pat = (r"(?:<!-- wp:quote -->\s*)?<blockquote\b[^>]*>(?:(?!</blockquote>).)*?" + re.escape(who) +
           r"(?:(?!</blockquote>).)*</blockquote>\s*(?:<!-- /wp:quote -->\s*)?")
    found = list(re.finditer(pat, raw, re.S))
    if len(found) != n:
        raise Drift(f"expected {n} quote(s) naming {who!r}, found {len(found)}")
    out, last = [], 0
    for m in found:
        out.append(raw[last:m.start()])
        last = m.end()
    out.append(raw[last:])
    return "".join(out), [m.group(0) for m in found]


LD_BLOCK = re.compile(r'(<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>)(.*?)(</script>)', re.S | re.I)
IMG_KEYS = ("url", "contentUrl", "thumbnailUrl")


def _is_data_image(o):
    return isinstance(o, dict) and any(isinstance(o.get(k), str) and o[k].startswith("data:") for k in IMG_KEYS)


def _strip(o):
    """Copy of a JSON value without data: images. Returns (copy, how many were removed)."""
    n = 0
    if isinstance(o, list):
        out = []
        for v in o:
            if _is_data_image(v) or (isinstance(v, str) and v.startswith("data:")):
                n += 1
                continue
            c, k = _strip(v)
            out.append(c)
            n += k
        return out, n
    if isinstance(o, dict):
        out = {}
        for key, v in o.items():
            if _is_data_image(v) or (isinstance(v, str) and v.startswith("data:image")):
                n += 1
                continue
            c, k = _strip(v)
            n += k
            if key == "image" and isinstance(v, list) and not c:
                continue                                            # every image in the list was a data: URI
            out[key] = c
        return out, n
    return o, 0


def strip_schema_images(raw):
    """Remove data: image entries from the page's JSON-LD blocks; every other byte of the page stays."""
    out, last, removed = [], 0, 0
    for m in LD_BLOCK.finditer(raw):
        body = m.group(2)
        if "data:image" not in body:
            continue
        try:
            obj = json.loads(body)
        except ValueError as e:
            raise Drift(f"a JSON-LD block does not parse: {e}")
        new, n = _strip(obj)
        if not n:
            raise Drift("a data: image sits somewhere this script does not know how to remove")
        lead, trail = body[:len(body) - len(body.lstrip())], body[len(body.rstrip()):]
        pretty = "\n" in body.strip()
        text = json.dumps(new, ensure_ascii=False, indent=2) if pretty else json.dumps(new, ensure_ascii=False, separators=(",", ":"))
        out += [raw[last:m.start(2)], lead + text + trail]
        last, removed = m.end(2), removed + n
    if not removed:
        raise Drift("no data: image found in any JSON-LD block")
    new_raw = "".join(out) + raw[last:]
    if "data:image" in new_raw:
        raise Drift("a data: image is still present after the edit")
    return new_raw, [f"{removed} embedded base64 image entr{'y' if removed == 1 else 'ies'} removed from the JSON-LD"]


def drop_faq_entry(raw, needle):
    """Remove the one FAQPage question whose answer contains `needle`, so structured data does not keep a claim the page no longer makes.
    The block keeps its format (compact or pretty); every other question stays. Exactly one entry must match."""
    out, last, removed = [], 0, []
    for m in LD_BLOCK.finditer(raw):
        body = m.group(2)
        if needle not in body:
            continue
        try:
            obj = json.loads(body)
        except ValueError as e:
            raise Drift(f"a JSON-LD block does not parse: {e}")
        items = obj.get("mainEntity") if isinstance(obj, dict) and obj.get("@type") == "FAQPage" else None
        if not isinstance(items, list):
            raise Drift("the text is in a JSON-LD block that is not an FAQPage question list")
        keep = []
        for q in items:
            ans = ((q or {}).get("acceptedAnswer") or {}).get("text", "") if isinstance(q, dict) else ""
            if isinstance(ans, str) and needle in ans:
                removed.append((q.get("name", ""), ans))
            else:
                keep.append(q)
        if not keep:
            raise Drift("removing the entry would leave the FAQ empty")
        obj["mainEntity"] = keep
        lead, trail = body[:len(body) - len(body.lstrip())], body[len(body.rstrip()):]
        pretty = "\n" in body.strip()
        text = json.dumps(obj, ensure_ascii=False, indent=2) if pretty else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        out += [raw[last:m.start(2)], lead + text + trail]
        last = m.end(2)
    if len(removed) != 1:
        raise Drift(f"expected exactly 1 FAQ entry whose answer contains {needle!r}, found {len(removed)}")
    new_raw = "".join(out) + raw[last:]
    if needle in new_raw:
        raise Drift(f"{needle!r} is still on the page outside that FAQ entry")
    return new_raw, [f"FAQ schema entry removed: question {removed[0][0]!r}; its answer: {removed[0][1]}"]


BROKEN_FIGURE = re.compile(r'<figure\b[^>]*>\s*<img\b[^>]*\bsrc="image/(?:jpeg|png|webp|gif);base64,([A-Za-z0-9+/=]+)"[^>]*>'
                           r'(?:\s*<figcaption>.*?</figcaption>)?\s*</figure>\n?', re.S)


def strip_broken_figures(raw, expect):
    found = list(BROKEN_FIGURE.finditer(raw))
    if len(found) != expect:
        raise Drift(f"expected {expect} broken base64 figures, found {len(found)}")
    images = []
    for m in found:
        try:
            images.append(base64.b64decode(m.group(1)))
        except ValueError:
            images.append(b"")
    return BROKEN_FIGURE.sub("", raw), [f"{len(found)} broken base64 figure(s) removed ({sum(len(i) for i in images):,} bytes of image data)"], images


def transform(raw, edits):
    """Apply a page's edits in order. Returns (new_raw, removed snippets or notes, decoded images). Raises Drift."""
    notes, images = [], []
    for e in edits:
        # quote / schema / figures edits are repeatable: when nothing of theirs is left on the page they are already done.
        # Text cuts stay strict, so a page that changed underneath the script is refused, never guessed at.
        if e[0] == "cut":
            raw, n = cut_text(raw, e[1])
        elif e[0] == "sub":
            raw, n = sub_text(raw, e[1], e[2])
        elif e[0] == "quote":
            visible = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
            if e[1] not in visible and e[1] not in html.unescape(visible):
                n = [f"already done: no quote naming {e[1]} left"]
            else:
                raw, n = cut_quote(raw, e[1], e[2])
        elif e[0] == "faq_drop":
            if e[1] not in raw:
                n = [f"already done: no FAQ schema entry mentions {short(e[1], 40)!r}"]
            else:
                raw, n = drop_faq_entry(raw, e[1])
        elif e[0] == "schema":
            if "data:image" not in raw:
                n = ["already done: no embedded image data left in the schema"]
            else:
                raw, n = strip_schema_images(raw)
        elif e[0] == "figures":
            if "base64," not in raw:
                n = ["already done: no broken base64 figures left"]
            else:
                raw, n, images = strip_broken_figures(raw, e[1])
        else:
            raise ValueError(e)
        notes += n
    return raw, notes, images


def claims_in(raw):
    """What the Index Engine's own trust scan still finds in this text."""
    text = html.unescape(raw)
    return [(label, m.group(0)) for label, rx in audit.TRUST_RE for m in rx.finditer(text)]


def check_result(page, before, after):
    """Problems with the intended new text, or []."""
    problems = []
    left = claims_in(after)
    if left and page.get("check_trust", page["batch"] == 1):
        problems.append(f"the trust scan still matches: {left[:2]}")
    for m in LD_BLOCK.finditer(after):
        body = m.group(2)
        try:
            json.loads(body)
        except ValueError as e:
            if body in before:
                continue                                            # an untouched block that was already invalid: not made worse
            problems.append(f"a JSON-LD block no longer parses: {e}")
    if page.get("schema") and "data:image" in after:
        problems.append("a data: image is still present")
    for who in [e[1] for e in page["edits"] if e[0] == "quote"]:
        if re.search(r"<blockquote\b(?:(?!</blockquote>).)*" + re.escape(who), after, re.S):
            problems.append(f"a quote naming {who} is still there")
    if len(after) >= len(before):
        problems.append("the page did not get smaller")
    return problems


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def short(s, n=300):
    s = re.sub(r"base64,[A-Za-z0-9+/=]{40,}", "base64,<…>", s)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- live helpers

def served(path, plain=False):
    return requests.get(BASE + path, params=None if plain else {"nc": secrets.token_hex(4)}, headers=UA, timeout=180)


def guard_run():
    """(exit code 0 CLEAN / 1 ALERT / 2 BLOCK, set of its ALERT/BLOCK lines)."""
    if not GUARD.exists():
        return None, set()
    r = subprocess.run([sys.executable, str(GUARD), "check"], cwd=str(GUARD.parent.parent), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    lines = {ln.strip() for ln in (r.stdout + r.stderr).splitlines() if re.search(r"\b(BLOCK|ALERT)\b", ln)}
    return r.returncode, lines


GUARD_WORDS = {0: "CLEAN", 1: "ALERT", 2: "BLOCK"}


def log_change(line):
    if not CHANGELOG.exists():
        print("  (CHANGELOG-AGENTS.md not found; add this entry by hand)\n  " + line)
        return
    txt = CHANGELOG.read_text(encoding="utf-8")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    head = f"## {today} (owner-run recover-first cleanup)\n\n"
    marker = "Read this before you start: it is how the next agent learns what the last one did.\n"
    if head in txt:
        txt = txt.replace(head, head + line + "\n", 1)
    elif marker in txt:
        txt = txt.replace(marker, marker + "\n" + head + line + "\n", 1)
    else:
        print("  (changelog layout not recognised; add this entry by hand)\n  " + line)
        return
    CHANGELOG.write_text(txt, encoding="utf-8")


def load_client():
    c = wp.WPClient(SITE)
    if not c.ok:
        sys.exit("No working WordPress login for gearuptofit.com (check 01-gearuptofit\\credentials.txt). Nothing was changed.")
    return c


def already_clean(p, raw):
    """True when everything this script would remove from the page is already gone (a re-run after a canary, or after a rollback-free apply)."""
    visible = html.unescape(re.sub(r"<script.*?</script>", " ", raw, flags=re.S))
    if any(g in visible for g in p["gone"]):
        return False
    if p.get("schema") and "data:image" in raw:
        return False
    if any(e[0] == "figures" for e in p["edits"]) and "base64," in raw:
        return False
    if any(e[0] == "faq_drop" and e[1] in raw for e in p["edits"]):            # the claim may only be left in structured data
        return False
    return True


def plan_pages(wpc, batch, only):
    plans = []
    for p in [p for p in PAGES if (batch is None or p["batch"] == batch) and (not only or p["id"] in only)]:
        try:
            raw, mod = wpc.raw(p["id"], "posts")
        except Exception as e:
            print(f"SKIP  {p['path']}: could not read it ({str(e)[:100]})")
            continue
        if already_clean(p, raw):
            print(f"DONE  {p['path']}: already clean, nothing to do")
            continue
        try:
            new, notes, images = transform(raw, p["edits"])
            problems = check_result(p, raw, new)
            if problems:
                raise Drift("; ".join(problems))
        except Drift as e:
            print(f"SKIP  {p['path']}: {e}. The page changed since this script was written; nothing was touched.")
            continue
        plans.append(dict(page=p, raw=raw, new=new, notes=notes, images=images, modified=mod))
    plans.sort(key=lambda x: len(x["raw"]))                       # smallest first: the canary is the cheapest page to put back
    return plans


def show(plans):
    for x in plans:
        p = x["page"]
        print(f"\n[{p['id']}] {p['path']}\n    why:  {p['why']}\n    size: {len(x['raw']):,} -> {len(x['new']):,} characters ({len(x['new']) - len(x['raw']):+,})")
        for n in x["notes"]:
            print(("    " if n.startswith("already done") else "    cuts: ") + short(n))


# ---------------------------------------------------------------- apply / rollback

def put(wpc, pid, text):
    wpc.update(pid, "posts", text)
    after, _ = wpc.raw(pid, "posts")
    return after == text


def apply_batch(wpc, plans, args):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"backup-batch{args.batch}-{stamp}.json"
    backup.write_text(json.dumps({"site": SITE, "batch": args.batch, "created": stamp, "pages": [
        dict(id=x["page"]["id"], type="posts", path=x["page"]["path"], modified_gmt=x["modified"], sha256_before=sha(x["raw"]),
             sha256_after=sha(x["new"]), raw_before=x["raw"]) for x in plans]}, ensure_ascii=False), encoding="utf-8")
    check = json.loads(backup.read_text(encoding="utf-8"))
    assert len(check["pages"]) == len(plans) and all(sha(c["raw_before"]) == c["sha256_before"] for c in check["pages"]), "backup did not verify"
    for x in plans:
        for i, img in enumerate(x["images"]):
            if img:
                (BACKUP_DIR / f"image-{x['page']['id']}-{i + 1}-{stamp}.jpg").write_bytes(img)
    print(f"\nBackup written and verified: {backup}")
    print(f"Undo everything in this batch with:\n  python \"{Path(__file__).resolve()}\" --rollback \"{backup}\"")

    code_before, alerts_before = guard_run()
    print(f"\nsite_guard before: {GUARD_WORDS.get(code_before, code_before)}")
    done = []
    for n, x in enumerate(plans):
        p = x["page"]
        print(f"\n{'CANARY' if n == 0 else 'page  '} [{p['id']}] {p['path']}")
        if not put(wpc, p["id"], x["new"]):
            back = "was put back" if put(wpc, p["id"], x["raw"]) else "COULD NOT BE VERIFIED AS RESTORED: check it by hand"
            sys.exit(f"  WordPress did not store exactly the intended text; this page {back}, and the batch stopped. "
                     f"Pages already changed: {[d['page']['id'] for d in done]}. Undo with the rollback command above (it also restores this page).")
        r = served(p["path"])
        if r.status_code != 200:
            back = "was put back" if put(wpc, p["id"], x["raw"]) else "COULD NOT BE VERIFIED AS RESTORED: check it by hand"
            sys.exit(f"  The page answered HTTP {r.status_code} after the edit; this page {back}, and the batch stopped. "
                     f"Pages already changed: {[d['page']['id'] for d in done]}. Undo with the rollback command above.")
        text = html.unescape(r.text)
        waiting = [g for g in p["gone"] if g in text]
        print(f"  stored exactly as intended; served page (?nc=) HTTP 200, {len(r.content):,} bytes"
              + (f"; still shows {waiting} (origin cache not refreshed yet: re-check in a few minutes)" if waiting else "; the removed text is gone"))
        done.append(x)
        if n == 0 and args.canary_only:
            print("\nStopped after the canary, as asked (--canary-only). Run again without it to do the rest of the batch.")
            break
        if n == 0 and len(plans) > 1:
            print("  canary verified; continuing in 15 s (Ctrl+C now to stop and keep only this page)...")
            time.sleep(15)

    code_after, alerts_after = guard_run()
    new_alerts = sorted(alerts_after - alerts_before)
    print(f"\nsite_guard after: {GUARD_WORDS.get(code_after, code_after)}"
          + (f"; {len(new_alerts)} new line(s):\n  " + "\n  ".join(new_alerts[:8]) if new_alerts else "; nothing new"))
    ids = ", ".join(str(x["page"]["id"]) for x in done)
    log_change(f"- {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ} | Owner, running gutf_recover_20261028.py batch {args.batch} (prepared by Claude from the Index Engine's "
               f"Revival analysis) | CONTENT SAVE of {len(done)} posts (ids {ids}): invented first-hand claims, invented expert quotes/bylines cut out; base64 "
               f"images removed from JSON-LD / broken figures removed; nothing added, no other text changed | where: the posts' stored content | backup + rollback: "
               f"{backup} (--rollback) | verified: REST read-back equals the intended text; ?nc= copy HTTP 200; site_guard.py check {GUARD_WORDS.get(code_after, code_after)}")
    print(f"\nChangelog entry added. Done: {len(done)} page(s).")
    if code_after == 2 and code_before != 2:
        print("\nsite_guard reports a BLOCK that was not there before: rolling this batch back.")
        sys.exit(3 if rollback(wpc, backup, force=True) else 2)     # 2 = rolled back cleanly, 3 = some pages need a look
    print("\nWhat visitors and Bing see (plain URL):")
    for x in done:
        r = served(x["page"]["path"], plain=True)
        age = r.headers.get("age")
        when = ""
        if r.headers.get("cf-cache-status") == "HIT" and age and age.isdigit():
            when = f"; cached copy refreshes by about {(dt.datetime.now() + dt.timedelta(seconds=max(0, 4 * 3600 - int(age)))):%H:%M}"
        print(f"  {r.status_code} cf-cache-status={r.headers.get('cf-cache-status')}{when}  {x['page']['path']}")
    print("\nNext: purge Seraphinite then Cloudflare if you want it sooner. If site_guard alerts only on these pages and you accept that,\n"
          "run `python site_guard\\site_guard.py snapshot`. Do not resubmit these URLs for indexing.")


PASTED_IMAGE = re.compile(r"image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{2000,}")


def verify_live(batch, only):
    """Read-only check of the live pages: how many are clean, and what the plain URL (what visitors and Bing get) still serves.
    Returns the number of pages whose ?nc= copy is not clean yet."""
    todo = [p for p in PAGES if (batch is None or p["batch"] == batch) and (not only or p["id"] in only)]
    pending = visible = 0
    watches_images = lambda p: bool(p.get("schema")) or any(e[0] == "figures" for e in p["edits"])      # noqa: E731
    print(f"{'?nc= copy':34} {'plain URL (visitors, Bing)':36} page")
    for p in todo:
        try:
            r = served(p["path"])
            text = html.unescape(r.text)
            left = [g for g in p["gone"] if g in text]
            # only whole pasted images count: the theme's own tiny inline icons are not the defect
            data_uris = len(PASTED_IMAGE.findall(r.text)) if p.get("schema") else 0
            broken = len(re.findall(r'src="image/[a-z]+;base64', r.text)) if any(e[0] == "figures" for e in p["edits"]) else 0
            clean = r.status_code == 200 and not left and not data_uris and not broken
            nc = f"{'CLEAN' if clean else 'NOT CLEAN'} {r.status_code} {len(r.content) // 1024:,} KB" + (f" still:{left}" if left else "") \
                 + (f" data-URIs:{data_uris}" if data_uris else "") + (f" broken-imgs:{broken}" if broken else "")
            pending += not clean
            pl = served(p["path"], plain=True)
            plain_text = html.unescape(pl.text)
            old = [g for g in p["gone"] if g in plain_text]
            heavy = len(PASTED_IMAGE.findall(pl.text)) if watches_images(p) else 0
            shows_new = pl.status_code == 200 and not old and not heavy
            visible += shows_new
            age = pl.headers.get("age")
            pn = f"{'cleaned' if shows_new else 'old cached copy'} {pl.status_code} {pl.headers.get('cf-cache-status')}" \
                 + (f" age {int(age) // 60} min" if age and age.isdigit() else "")
        except Exception as e:
            nc, pn = f"ERROR {type(e).__name__}", ""
            pending += 1
        print(f"{nc[:34]:34} {pn[:36]:36} [{p['id']}] {p['path']}")
    print(f"\n{len(todo) - pending} of {len(todo)} pages clean on the ?nc= copy (the server's own copy).")
    print(f"{visible} of {len(todo)} already show the cleaned page on the plain URL (what visitors and Bing get); the rest still serve "
          "Cloudflare's older copy and refresh by themselves as each cache entry expires (about 4 hours after its last refresh).")
    return pending


def rollback(wpc, backup_path, force=False):
    data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    bad = 0
    for p in data["pages"]:
        cur, _ = wpc.raw(p["id"], p["type"])
        if sha(cur) == p["sha256_before"]:
            print(f"  [{p['id']}] {p['path']}: already as before")
            continue
        if sha(cur) != p["sha256_after"] and not force:
            print(f"  [{p['id']}] {p['path']}: EDITED SINCE this script ran; left alone (add --force to overwrite it anyway)")
            bad += 1
            continue
        if put(wpc, p["id"], p["raw_before"]):
            print(f"  [{p['id']}] {p['path']}: restored")
        else:
            print(f"  [{p['id']}] {p['path']}: WordPress did not store the restored text; check this page by hand")
            bad += 1
    log_change(f"- {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ} | Owner, running gutf_recover_20261028.py --rollback | ROLLBACK of {len(data['pages'])} posts to "
               f"their text before the cleanup | backup used: {backup_path} | verified: REST read-back equals the backup" + (f"; {bad} page(s) not restored" if bad else ""))
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--batch", type=int, choices=(1, 2, 3, 4, 5, 6), help="1 = the 8 pages earning Bing clicks, 2 = 7 pages with quotes and embedded image data, "
                                                                         "3-5 = the other pages the trust scan flags (second wave), "
                                                                         "6 = the same invented claim left in one page's FAQ schema")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--only", type=int, nargs="+", help="post ids to include (default: the whole batch)")
    ap.add_argument("--canary-only", action="store_true", help="with --apply: stop after the first page")
    ap.add_argument("--rollback", metavar="BACKUP_JSON", help="restore the pages recorded in a backup file")
    ap.add_argument("--force", action="store_true", help="with --rollback: also overwrite pages edited since")
    ap.add_argument("--verify", action="store_true", help="read-only: check the live pages (?nc= copy and plain URL), no login needed")
    args = ap.parse_args()
    if args.verify:
        sys.exit(1 if verify_live(args.batch, args.only) else 0)
    wpc = load_client()
    if args.rollback:
        sys.exit(1 if rollback(wpc, args.rollback, args.force) else 0)
    if args.apply and not args.batch:
        sys.exit("Choose a batch: --batch 1 or --batch 2 (never more than 8 pages at a time).")
    plans = plan_pages(wpc, args.batch, args.only)
    if not plans:
        sys.exit("Nothing to do.")
    if args.apply and len(plans) > MAX_PER_BATCH:
        sys.exit(f"{len(plans)} pages selected; this script stops at {MAX_PER_BATCH} per batch (the site's limit is 10).")
    show(plans)
    if not args.apply:
        print(f"\nDRY RUN: {len(plans)} page(s), nothing was written. To apply:\n  python \"{Path(__file__).resolve()}\" --batch N --apply")
        return
    apply_batch(wpc, plans, args)


if __name__ == "__main__":
    main()
