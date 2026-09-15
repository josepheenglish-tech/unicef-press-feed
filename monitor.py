#!/usr/bin/env python3
"""
UNICEF press monitor: collect configured office listings into a page and RSS feed.

For each office in offices.json:
  1. Prefer a declared RSS or Atom feed on a reachable listing page.
  2. Otherwise parse listing cards, trying localized press-centre fallbacks.
  3. Follow a bounded number of next-page links and report incomplete collection.

Then:
  - retain each source/URL record and its first observation for 180 days
  - preserve prior records when a listing fails or no longer contains them
  - write data.json, feed.xml (press releases only), and index.html

Usage:
    pip install requests beautifulsoup4
    python monitor.py                  # full run
    python monitor.py --offices india,southsudan,mali    # test a few
    python monitor.py --workers 8      # default 6

Outputs land in ./public/ — serve that directory, or push it to GitHub Pages.
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import unicodedata
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import format_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.unicef.org/"
TIMEOUT = 15
RETENTION_DAYS = 180
USER_AGENT = "Mozilla/5.0 (compatible; UNICEF-press-monitor/2.0; internal media monitoring)"

# Item links on UNICEF Drupal sites sit under one of these path segments.
ITEM_PATH = re.compile(
    r"/(press-releases?|statements?|communiqu[eé]s?-de-presse|comunicados?-(de-)?prensa|"
    r"comunicados?-de-imprensa|press-release|remarks)/",
    re.I,
)

# Content-type labels as they appear on the listing pages.
TYPE_LABELS = {
    "press release": "Press release",
    "communiqué de presse": "Press release",
    "communique de presse": "Press release",
    "comunicado de prensa": "Press release",
    "comunicado de imprensa": "Press release",
    "statement": "Statement",
    "déclaration": "Statement",
    "declaración": "Statement",
    "remarks": "Remarks",
    "note to correspondents": "Note to correspondents",
}

MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
    # Portuguese
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "maio": 5, "junho": 6,
    "julho": 7, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

DATE_RE = re.compile(
    r"(\d{1,2})\s*(?:de\s+)?([A-Za-zÀ-ÿ]{3,12})\.?\s*(?:de\s+)?(\d{4})|"
    r"([A-Za-zÀ-ÿ]{3,12})\s+(\d{1,2}),?\s+(\d{4})|"
    r"(\d{4})-(\d{2})-(\d{2})"
)

local = threading.local()


# ---------------------------------------------------------------- helpers

def get(url):
    if not safe_url(url):
        return None
    if not hasattr(local, "session"):
        local.session = requests.Session()
        local.session.headers.update({"User-Agent": USER_AGENT})
    try:
        r = local.session.get(url, timeout=TIMEOUT, allow_redirects=False)
        for _ in range(5):
            if r.is_redirect:
                url = urljoin(r.url, r.headers.get("Location", ""))
                if not safe_url(url):
                    return None
                r = local.session.get(url, timeout=TIMEOUT, allow_redirects=False)
            else:
                break
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding == "ISO-8859-1" else r.encoding
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def safe_url(url):
    p = urlsplit(url)
    return p.scheme == "https" and p.hostname in {"www.unicef.org", "unicef.org", "www.unicef.cn", "unicef.cn"} and not p.username and p.port in (None, 443)


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def parse_date(text):
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        if m.group(1):
            day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
            month = MONTHS.get(month_name) or next((n for name, n in MONTHS.items() if name.startswith(month_name) and len(month_name) == 3), None)
            if not month:
                return None
            return dt.date(int(year), month, int(day))
        if m.group(4):
            month_name, day, year = m.group(4).lower(), m.group(5), m.group(6)
            month = MONTHS.get(month_name) or next((n for name, n in MONTHS.items() if name.startswith(month_name) and len(month_name) == 3), None)
            if not month:
                return None
            return dt.date(int(year), month, int(day))
        return dt.date(int(m.group(7)), int(m.group(8)), int(m.group(9)))
    except ValueError:
        return None


def detect_type(text):
    low = (text or "").lower()
    for key, label in TYPE_LABELS.items():
        if key in low:
            return label
    return "Press release"


def norm_title(title):
    """Normalise for cross-office duplicate detection."""
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = "".join(c if c.isalnum() else " " for c in t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- collection

def discover_feed(page, page_url):
    soup = BeautifulSoup(page.text, "html.parser")
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        if link.get("type", "") in ("application/rss+xml", "application/atom+xml"):
            return urljoin(page_url, link.get("href", ""))
    return None


def items_from_feed(feed_url, office):
    r = get(feed_url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    if root.tag.split("}")[-1] not in ("rss", "feed", "RDF"):
        return []
    out = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        tags = {n.tag.split("}")[-1]: n for n in node}
        title = "".join(tags["title"].itertext()).strip() if "title" in tags else ""
        links = [n for n in node if n.tag.split("}")[-1] == "link" and n.get("rel", "alternate") == "alternate"]
        link = (links[0].get("href") or links[0].text or "") if links else ""
        date_text = next((tags[k].text for k in ("pubDate", "published", "updated", "date") if k in tags), "")
        link = urljoin(feed_url, link)
        if not (title and safe_url(link) and ITEM_PATH.search(unquote(link))):
            continue
        out.append(make_item(office, title, urljoin(feed_url, link), parse_date(date_text),
                             "Press release", "feed"))
    return out


def items_from_listing(page, page_url, office):
    soup = BeautifulSoup(page.text, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if not safe_url(href) or not ITEM_PATH.search(unquote(href)):
            continue
        if a.find_parent(["nav", "header", "footer"]):
            continue
        heading = a.select_one("h2, h3, h4, .list-short-title, .tile--title")
        title = (heading or a).get_text(" ", strip=True)
        if len(title) < 12:
            continue
        if href in seen:
            continue
        seen.add(href)

        # Look upward for the card that holds the date and content-type label.
        node = a.find_parent(class_=re.compile(r"^(list-content|list-item|tile|views-row|card-content|card_large|card_small)$")) or a.parent
        context = node.get_text(" ", strip=True)
        date_node = node.select_one("time, .list-date, .tile--date, .date")
        date = parse_date(date_node.get("datetime") or date_node.get_text(" ", strip=True)) if date_node else parse_date(context)
        label = node.select_one(".content-category-content, .tile--content-category")
        item = make_item(office, title, canonical(href), date,
                         detect_type(label.get_text(" ", strip=True) if label else context), "scrape")
        picture = node.select_one("img") or (node.parent.select_one("img") if node.parent else None)
        if picture:
            img_url = urljoin(page_url, picture.get("src") or picture.get("data-src") or "")
            if safe_url(img_url) and not img_url.endswith(".svg"):
                item["image"] = img_url
        out.append(item)
    return out


def make_item(office, title, url, date, kind, method):
    return {
        "office": office["name"],
        "slug": office["slug"],
        "region": office["region"],
        "lang": office.get("lang", "en"),
        "title": title,
        "url": url,
        "date": date.isoformat() if date else None,
        "type": kind,
        "method": method,
    }


def collect(office, pages=2):
    site = office.get("site", BASE + office["slug"] + "/")
    attempted, reachable = [], False
    localized = {"fr": "centre-de-presse", "es": "centro-de-prensa", "pt": "centro-de-imprensa"}
    paths = list(dict.fromkeys(office.get("listing", ["press-releases"]) +
                 [localized.get(office.get("lang"), "press-centre"), "en/press-centre", "press-centre", "media"]))
    for path in paths:
        page_url = urljoin(site, path)
        attempted.append(page_url)
        page = get(page_url)
        if not page:
            continue
        reachable = True
        page_url = page.url
        feed_url = discover_feed(page, page_url)
        if feed_url:
            items = items_from_feed(feed_url, office)
            if items:
                return items, page_url, "feed"
        items = items_from_listing(page, page_url, office)
        if items:
            visited = {page_url}
            for _ in range(pages - 1):
                soup = BeautifulSoup(page.text, "html.parser")
                next_link = soup.select_one('a[rel="next"], .pager__item--next a')
                if not next_link:
                    next_link = next((a for a in soup.select('.pager a[href]') if a.get_text(' ', strip=True).lower() == 'next'), None)
                if not next_link:
                    break
                next_url = urljoin(page.url, next_link.get("href", "")).split("#")[0]
                if next_url in visited or not safe_url(next_url):
                    break
                visited.add(next_url)
                time.sleep(0.25)
                page = get(next_url)
                if page is None:
                    return items, page_url, "partial"
                batch = items_from_listing(page, page.url, office)
                if not batch:
                    return items, page_url, "partial"
                items.extend(batch)
            return items, page_url, "scrape"
    return [], attempted[-1] if attempted else site, "no_items" if reachable else "failed"


# ---------------------------------------------------------------- output

def build_rss(items, generated, site_url=""):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>UNICEF press feed</title>",
        f"<link>{html.escape(site_url or 'https://www.unicef.org/media/press-centre')}</link>",
        "<description>Press releases from monitored UNICEF offices. Coverage varies by source.</description>",
        f"<lastBuildDate>{format_datetime(generated)}</lastBuildDate>",
    ]
    unique = {}
    for item in items:
        if item['type'] == 'Press release':
            unique.setdefault(canonical(item['url']), item)
    for it in list(unique.values())[:1000]:
        pub = ""
        if it["date"]:
            d = dt.datetime.fromisoformat(it["date"]).replace(tzinfo=dt.timezone.utc)
            pub = f"<pubDate>{format_datetime(d)}</pubDate>"
        parts.append(
            "<item>"
            f"<title>{html.escape(it['title'])}</title>"
            f"<link>{html.escape(it['url'])}</link>"
            f"<guid isPermaLink='true'>{html.escape(it['url'])}</guid>"
            f"<category>{html.escape(it['office'])}</category>"
            f"<category>{html.escape(it['region'])}</category>"
            f"{pub}"
            f"<description>{html.escape(it['office'])} — {html.escape(it['type'])}</description>"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts)


def build_page(payload, template_path="template.html"):
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    return template.replace("/*DATA*/null/*DATA*/", json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))


# ---------------------------------------------------------------- persistence and entry point

ROOT = Path(__file__).resolve().parent


def merge_archive(previous, incoming, now):
    records = {f"{it['slug']}|{canonical(it['url'])}": dict(it) for it in previous}
    for it in incoming:
        key = f"{it['slug']}|{canonical(it['url'])}"
        before = records.get(key, {})
        records[key] = {**it, "first_seen": before.get("first_seen", now.isoformat()),
                        "last_seen": now.isoformat()}
        if not it.get("date") and before.get("date"):
            records[key]["date"] = before["date"]
    cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).date().isoformat()
    return sorted((it for it in records.values()
                   if (it.get("date") or it["first_seen"][:10]) >= cutoff),
                  key=lambda x: (bool(x.get("date")), x.get("date") or x["first_seen"][:10], x["url"]), reverse=True)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def write_outputs(payload, out, site_url=""):
    out.mkdir(parents=True, exist_ok=True)
    payload["items"].sort(key=lambda x: (bool(x.get("date")), x.get("date") or x["first_seen"][:10], x["url"]), reverse=True)
    atomic_write(out / "data.json", json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write(out / "feed.xml", build_rss(payload["items"], dt.datetime.fromisoformat(payload["generated"]), site_url))
    atomic_write(out / "index.html", build_page(payload, ROOT / "template.html"))


def main():
    ap = argparse.ArgumentParser(description="Collect public UNICEF press listings into a persistent feed.")
    ap.add_argument("--offices", help="Comma-separated source slugs; other sources and their history are preserved")
    ap.add_argument("--workers", type=int, default=6, choices=range(1, 13), metavar="1-12")
    ap.add_argument("--pages", type=int, default=2, choices=range(1, 51), metavar="1-50")
    ap.add_argument("--site-url", default="", help="Team site's public base URL for RSS metadata")
    ap.add_argument("--render-only", action="store_true", help="Rebuild page from existing collected data without fetching")
    args = ap.parse_args()
    out = ROOT / "public"
    archive_path = ROOT / "state.json"
    if args.render_only:
        payload = json.loads((out / "data.json").read_text(encoding="utf-8"))
        write_outputs(payload, out, args.site_url)
        return

    registry = json.loads((ROOT / "offices.json").read_text(encoding="utf-8"))["offices"]
    selected = registry
    if args.offices:
        wanted = {s.strip() for s in args.offices.split(",")}
        unknown = wanted - {o["slug"] for o in registry}
        if unknown:
            ap.error("Unknown offices: " + ", ".join(sorted(unknown)))
        selected = [o for o in registry if o["slug"] in wanted]
    previous = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {}
    if previous and previous.get("version") != 2:
        raise SystemExit("State format differs. Keep a backup and use an empty state.json for this version.")
    now = dt.datetime.now(dt.timezone.utc)
    sources = {s["slug"]: dict(s) for s in previous.get("sources", [])}
    incoming = []
    for office in registry:
        sources.setdefault(office["slug"], {
            "office": office["name"], "slug": office["slug"], "region": office["region"],
            "url": office.get("site", BASE + office["slug"] + "/"), "status": "unchecked",
            "method": "unchecked", "count": 0, "checked": None, "last_success": None,
        })
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect, o, args.pages): o for o in selected}
        for fut in concurrent.futures.as_completed(futures):
            office = futures[fut]
            error = ""
            try:
                items, url, method = fut.result()
            except Exception as exc:
                items, url, method = [], sources[office["slug"]]["url"], "failed"
                error = type(exc).__name__
            status = "ok" if method in ("feed", "scrape") else method
            unique = {canonical(it["url"]): it for it in items}
            items = list(unique.values())
            source = sources[office["slug"]]
            source.update({"url": url, "method": method, "status": status, "count": len(items),
                           "checked": now.isoformat(), "error": error})
            if items:
                source["last_success"] = now.isoformat()
            incoming.extend(items)
            print(f"{office['name']:42s} {status:12s} {len(items):4d}", flush=True)

    archive = merge_archive(previous.get("items", []), incoming, now)
    for source in sources.values():
        dates = [it["date"] for it in archive if it["slug"] == source["slug"] and it.get("date")]
        source["last_item"] = max(dates, default=None)
        source["retained_count"] = sum(it["slug"] == source["slug"] for it in archive)
    payload = {
        "version": 2, "generated": now.isoformat(), "items": archive,
        "sources": sorted(sources.values(), key=lambda s: (s["region"], s["office"])),
        "retention_days": RETENTION_DAYS, "pages_per_source": args.pages,
        "scope": "Configured global, regional and country office listings; not a complete archive or all language editions.",
        "rss_limit": 1000,
    }
    # Persist history before publication so a failed host upload cannot erase prior releases.
    atomic_write(archive_path, json.dumps(payload, ensure_ascii=False, indent=2))
    write_outputs(payload, out, args.site_url)
    successful = sum(s["status"] == "ok" for s in sources.values())
    print(f"\n{len(archive)} retained items; {successful}/{len(registry)} sources OK.", flush=True)
    if not incoming:
        raise SystemExit("No items collected. Existing history was preserved; check source access.")


if __name__ == "__main__":
    main()
