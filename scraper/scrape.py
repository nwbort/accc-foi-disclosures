#!/usr/bin/env python3
"""Scrape the ACCC FOI disclosure log and archive every disclosure's documents.

Layout produced:

    disclosures/
      index.json                    # machine-readable index of every disclosure
      <year>/<id>/
        metadata.json               # title, description, dates, source URLs, files
        README.md                   # human-readable summary with links to files
        <document files>

Re-running is incremental: files already on disk (matched by source URL) are
not downloaded again. Disclosures that vanish from the ACCC site are kept.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.accc.gov.au"
LOG_URL = f"{BASE}/about-us/freedom-of-information/freedom-of-information-disclosure-log"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "disclosures"
UA = "Mozilla/5.0 (compatible; accc-foi-archiver; +https://github.com/nwbort/accc-foi-disclosures)"
DELAY = 1.0  # seconds between requests, be polite

session = requests.Session()
session.headers["User-Agent"] = UA


def get(url: str, **kw) -> requests.Response:
    for attempt in range(5):
        try:
            r = session.get(url, timeout=120, **kw)
            if r.status_code == 404:
                return r
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 4:
                raise
            wait = 5 * 2**attempt
            print(f"  retry {attempt + 1} for {url} in {wait}s: {e}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def text(el) -> str | None:
    if el is None:
        return None
    t = el.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", t) or None


def field_date(card, name: str) -> str | None:
    f = card.select_one(f".field--name-{name} time")
    if f is None:
        return None
    raw = f.get("datetime", "")
    try:
        # Drupal stores 12:00Z on the intended (AEST) day; the date part is what we want.
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text(f)


def slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "unknown"


def classify(title: str) -> tuple[str, str]:
    """Return (year_folder, id_folder) for a disclosure title.

    Handles "100482/2026-2027", "100105 - 2023-2024", "66-2019", "66-2010/2011".
    """
    yr = r"((?:19|20)\d{2})(?:\s*[-/]\s*((?:19|20)\d{2}))?"
    m = re.search(r"FOI Request\s+(\d+)\s*[/-]\s*" + yr, title, re.I)
    if m:
        return "-".join(filter(None, m.group(2, 3))), m.group(1)
    m = re.search(r"Proactive Disclosure\s+([A-Za-z0-9]+)\s*[/-]\s*" + yr, title, re.I)
    if m:
        return "-".join(filter(None, m.group(2, 3))), f"proactive-{m.group(1)}"
    m = re.search(r"((?:19|20)\d{2})", title)
    return (m.group(1) if m else "other"), slugify(title)


def safe_filename(name: str) -> str:
    name = unquote(name).replace("/", "-").replace("\\", "-")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".view-content article.accc-card"):
        a = card.select_one(".accc-card__title a")
        if a is None:
            continue
        title = text(a)
        body = card.select_one(".field--name-field-accc-body")
        files = []
        for fa in card.select(".field--name-field-acccgov-foi-attachments a[href]"):
            files.append(
                {
                    "title": text(fa.select_one(".accc-document-title")) or text(fa),
                    "size": text(fa.select_one(".accc-document-size")),
                    "url": urljoin(BASE, fa["href"]),
                }
            )
        out.append(
            {
                "title": title,
                "url": urljoin(BASE, a["href"]),
                "description": text(body),
                "post_date": field_date(card, "field-acccgov-foi-post-date"),
                "date_of_access": field_date(card, "field-acccgov-foi-date-access"),
                "files": files,
            }
        )
    return out


def scrape_log(max_pages: int | None = None) -> list[dict]:
    entries, seen = [], set()
    page = 0
    while True:
        print(f"Fetching listing page {page}")
        r = get(LOG_URL, params={"page": page, "sort_by": "accc_published_date"})
        items = parse_listing(r.text)
        new = [e for e in items if e["url"] not in seen]
        if not new:
            break
        for e in new:
            seen.add(e["url"])
        entries.extend(new)
        page += 1
        if max_pages and page >= max_pages:
            break
        time.sleep(DELAY)
    return entries


def write_readme(d: Path, meta: dict) -> None:
    lines = [f"# {meta['title']}", ""]
    if meta.get("description"):
        lines += [meta["description"], ""]
    lines += [
        f"- **Post date:** {meta.get('post_date') or 'n/a'}",
        f"- **Date of access:** {meta.get('date_of_access') or 'n/a'}",
        f"- **Source:** <{meta['url']}>",
        f"- **First archived:** {meta.get('first_seen')}",
        "",
        "## Documents",
        "",
    ]
    for f in meta["files"]:
        if f.get("local"):
            lines.append(f"- [{f['title']}](<{f['local']}>) ({f.get('size') or '?'})")
        else:
            lines.append(f"- {f['title']} — not downloaded ({f.get('error', 'missing')}) <{f['url']}>")
    if meta.get("removed_from_site"):
        lines += ["", f"_No longer listed on the ACCC site as of {meta['removed_from_site']}._"]
    (d / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


_claimed: set[Path] = set()


def resolve_dir(entry: dict) -> Path:
    """Folder for an entry. Distinct disclosures sharing a title get -2, -3... suffixes."""
    year, ident = classify(entry["title"])
    n = 1
    while True:
        d = OUT / year / (ident if n == 1 else f"{ident}-{n}")
        mp = d / "metadata.json"
        if mp.exists():
            if json.loads(mp.read_text()).get("url") == entry["url"]:
                return d
        elif d not in _claimed:
            return d
        n += 1


def archive(entry: dict, today: str, download: bool = True) -> tuple[dict, int]:
    d = resolve_dir(entry)
    _claimed.add(d)
    year, ident = d.parent.name, d.name
    d.mkdir(parents=True, exist_ok=True)
    meta_path = d / "metadata.json"
    old = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    old_files = {f["url"]: f for f in old.get("files", [])}

    meta = {
        **entry,
        "id": ident,
        "year": year,
        "path": str(d.relative_to(ROOT)),
        "first_seen": old.get("first_seen", today),
    }
    used_names = {f["local"] for f in old_files.values() if f.get("local")}
    files, downloaded = [], 0
    for f in entry["files"]:
        rec = dict(f)
        prev = old_files.get(f["url"])
        if prev and prev.get("local") and (d / prev["local"]).exists():
            rec["local"] = prev["local"]
            rec["downloaded"] = prev.get("downloaded")
        elif download:
            name = safe_filename(Path(urlparse(f["url"]).path).name)
            stem, suf = Path(name).stem, Path(name).suffix
            n = 2
            while name in used_names and not (prev and prev.get("local") == name):
                name = f"{stem} ({n}){suf}"
                n += 1
            print(f"  downloading {name}")
            try:
                r = get(f["url"])
                if r.status_code == 404:
                    raise requests.HTTPError("404 Not Found")
                (d / name).write_bytes(r.content)
                rec["local"] = name
                rec["downloaded"] = today
                used_names.add(name)
                downloaded += 1
            except requests.RequestException as e:
                rec["error"] = str(e)
                print(f"  FAILED {f['url']}: {e}", file=sys.stderr)
            time.sleep(DELAY)
        files.append(rec)
    # keep files that used to be listed but are no longer on the page
    current = {f["url"] for f in entry["files"]}
    for url, prev in old_files.items():
        if url not in current and prev.get("local"):
            files.append({**prev, "removed_from_site": prev.get("removed_from_site", today)})
    meta["files"] = files
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_readme(d, meta)
    return meta, downloaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-pages", type=int, help="only scan this many listing pages")
    ap.add_argument("--no-download", action="store_true", help="write metadata only")
    args = ap.parse_args()

    today = dt.date.today().isoformat()
    entries = scrape_log(args.max_pages)
    if not entries:
        print("No entries found — page layout may have changed.", file=sys.stderr)
        return 1
    print(f"Found {len(entries)} disclosures")

    OUT.mkdir(exist_ok=True)
    index_path = OUT / "index.json"
    old_index = json.loads(index_path.read_text()) if index_path.exists() else []
    index, new_disclosures, total_dl = {}, [], 0
    for e in entries:
        is_new = not (resolve_dir(e) / "metadata.json").exists()
        meta, n = archive(e, today, download=not args.no_download)
        total_dl += n
        if is_new:
            new_disclosures.append(meta["title"])
        index[meta["path"]] = meta

    # Disclosures no longer on the site: keep them, mark as removed.
    if not args.max_pages:
        for old in old_index:
            if old["path"] not in index:
                mp = ROOT / old["path"] / "metadata.json"
                if mp.exists():
                    m = json.loads(mp.read_text())
                    m.setdefault("removed_from_site", today)
                    mp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    write_readme(mp.parent, m)
                    index[old["path"]] = m
    else:
        for old in old_index:
            index.setdefault(old["path"], old)

    rows = sorted(index.values(), key=lambda m: (m.get("post_date") or "", m["path"]), reverse=True)
    summary = [
        {k: m.get(k) for k in ("title", "description", "post_date", "date_of_access", "url", "path", "first_seen", "removed_from_site")}
        | {"file_count": len(m["files"])}
        for m in rows
    ]
    index_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_index_md(rows)

    print(f"New disclosures: {len(new_disclosures)}; files downloaded: {total_dl}")
    for t in new_disclosures:
        print(f"  + {t}")
    return 0


def write_index_md(rows: list[dict]) -> None:
    lines = [
        "# ACCC FOI disclosure log archive",
        "",
        f"{len(rows)} disclosures. Source: <{LOG_URL}>",
        "",
        "| Post date | Disclosure | Description | Files |",
        "|---|---|---|---|",
    ]
    for m in rows:
        desc = (m.get("description") or "").replace("|", "\\|")
        if len(desc) > 140:
            desc = desc[:137] + "…"
        rel = Path(m["path"]).relative_to("disclosures").as_posix()
        flag = " _(removed from site)_" if m.get("removed_from_site") else ""
        lines.append(f"| {m.get('post_date') or ''} | [{m['title']}](<{rel}/>){flag} | {desc} | {len(m['files'])} |")
    (OUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
