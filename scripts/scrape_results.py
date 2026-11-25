#!/usr/bin/env python3
"""
Scrape county-level results from the Maryland State Board of Elections
results pages and write them to CSV.

Default target: 2026 Gubernatorial General Election (unofficial results).
  https://elections.maryland.gov/elections/2026/General_results/gen_results_2026_by_county_{1..24}.html

Output (in --out, default ./md_results):
  by_county/<NN>_<county>.csv   one file per county
  md_county_results_all.csv     all 24 counties stacked
  raw_html/                     cached pages (if --save-html)

Each row is one candidate (or ballot-question answer) in one contest in one county:
  county_code, county, contest, district, vote_for, reporting, candidate, party,
  early_voting, election_day, mail_in, provisional, total, pct, provisional_nr,
  last_refreshed, source_url, scraped_at

Usage:
  pip install requests beautifulsoup4
  python md_county_results.py                      # scrape all 24 counties
  python md_county_results.py --save-html          # also keep the raw pages
  python md_county_results.py --from-dir md_results/raw_html   # re-parse cached pages offline
  python md_county_results.py --counties 1 13 24   # just Allegany, Harford, Worcester
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL_TEMPLATE = (
    "https://elections.maryland.gov/elections/2026/General_results/"
    "gen_results_2026_by_county_{n}.html"
)

COUNTIES = {
    1: "Allegany", 2: "Anne Arundel", 3: "Baltimore City", 4: "Baltimore County",
    5: "Calvert", 6: "Caroline", 7: "Carroll", 8: "Cecil", 9: "Charles",
    10: "Dorchester", 11: "Frederick", 12: "Garrett", 13: "Harford", 14: "Howard",
    15: "Kent", 16: "Montgomery", 17: "Prince George's", 18: "Queen Anne's",
    19: "Saint Mary's", 20: "Somerset", 21: "Talbot", 22: "Washington",
    23: "Wicomico", 24: "Worcester",
}

FIELDS = [
    "county_code", "county", "contest", "district", "vote_for", "reporting",
    "candidate", "party", "early_voting", "election_day", "mail_in",
    "provisional", "total", "pct", "provisional_nr",
    "last_refreshed", "source_url", "scraped_at",
]

# Map the page's column headers onto our field names.
HEADER_MAP = {
    "name": "candidate",
    "candidate": "candidate",
    "question": "candidate",
    "response": "candidate",
    "party": "party",
    "early voting": "early_voting",
    "election day": "election_day",
    "mail-in ballot": "mail_in",
    "mail-in": "mail_in",
    "mail in ballot": "mail_in",
    "provisional": "provisional",
    "total": "total",
    "percentage": "pct",
    "percent": "pct",
}

VOTE_FOR_RE = re.compile(r"^\s*vote\s+for\s+(\w+)", re.I)
REPORTING_RE = re.compile(r"\(?\s*(\d[\d,]*\s+of\s+\d[\d,]*\s+[^)]*?reported)\s*\)?", re.I)
REFRESHED_RE = re.compile(r"Last refreshed:\s*([0-9/]+\s+[0-9:]+\s*[AP]M)", re.I)

HEADERS = {"User-Agent": "Mozilla/5.0 (research scraper; CNS Maryland)"}


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def to_number(text):
    """'1,275' -> 1275 ; '31.26%' -> 31.26 ; 'NR'/'' -> None"""
    t = clean(text).replace(",", "").replace("%", "")
    if t == "" or t.upper() == "NR":
        return None
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return None


def fetch(url, retries=3, pause=2.0):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == retries:
                raise
            print(f"  retry {attempt} for {url}: {e}", file=sys.stderr)
            time.sleep(pause * attempt)


def table_header(table):
    """Return list of mapped field names for a table's columns, or None if it
    isn't a results table."""
    head_row = None
    thead = table.find("thead")
    if thead:
        head_row = thead.find("tr")
    if head_row is None:
        for tr in table.find_all("tr"):
            if tr.find("th"):
                head_row = tr
                break
    if head_row is None:
        return None
    labels = [clean(c.get_text(" ")).lower() for c in head_row.find_all(["th", "td"])]
    mapped = [HEADER_MAP.get(l) for l in labels]
    # Must look like a results table: a name column and a total column.
    if "candidate" not in mapped or "total" not in mapped:
        # Some question tables have a blank first header cell for Yes/No.
        if "total" in mapped and mapped and mapped[0] is None:
            mapped[0] = "candidate"
        else:
            return None
    return mapped, head_row


def parse_page(html, county_code, source_url, scraped_at):
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ")
    m = REFRESHED_RE.search(page_text)
    last_refreshed = m.group(1) if m else ""

    county = COUNTIES.get(county_code, "")
    rows = []
    contest = district = vote_for = reporting = ""

    # find_all returns elements in document order, so we can walk headings,
    # notes and tables as a stream and keep track of where we are.
    for el in soup.find_all(["h2", "h3", "h4", "h5", "p", "span", "div", "table"]):
        if el.name == "table":
            hdr = table_header(el)
            if not hdr:
                continue
            fields, head_row = hdr
            for tr in el.find_all("tr"):
                if tr is head_row or tr.find_parent("thead"):
                    continue
                cells = tr.find_all(["td", "th"])
                if not cells or len(cells) < 2:
                    continue
                rec = {k: "" for k in FIELDS}
                rec.update(
                    county_code=f"{county_code:02d}", county=county, contest=contest,
                    district=district, vote_for=vote_for, reporting=reporting,
                    last_refreshed=last_refreshed, source_url=source_url,
                    scraped_at=scraped_at,
                )
                for f, c in zip(fields, cells):
                    if not f:
                        continue
                    raw = clean(c.get_text(" "))
                    if f in ("candidate", "party"):
                        rec[f] = raw
                    else:
                        rec[f] = to_number(raw)
                        if f == "provisional" and raw.upper() == "NR":
                            rec["provisional_nr"] = True
                if not rec["candidate"] or rec["candidate"].lower() in ("total", "totals"):
                    continue
                rec["provisional_nr"] = bool(rec["provisional_nr"])
                rows.append(rec)
            continue

        # Skip containers that hold tables/headings themselves; we only want leaf text.
        if el.name in ("div", "span", "p") and el.find(["table", "h2", "h3", "h4", "h5"]):
            continue
        if el.find_parent("table"):
            continue
        text = clean(el.get_text(" "))
        if not text:
            continue

        vf = VOTE_FOR_RE.match(text)
        rp = REPORTING_RE.search(text)
        if vf:
            vote_for = vf.group(1)
            if rp:
                reporting = rp.group(1)
        elif rp and el.name in ("p", "span", "div", "h4", "h5"):
            reporting = rp.group(1)
        elif el.name == "h2":
            contest, district, vote_for, reporting = text, "", "", ""
        elif el.name in ("h3", "h4") and len(text) < 120:
            # District / sub-contest heading (e.g. "District 1A", "Council District 3")
            district, vote_for, reporting = text, "", ""

    return rows


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/2026/results", help="output folder")
    ap.add_argument("--counties", type=int, nargs="*", default=list(COUNTIES),
                    help="county codes 1-24 (default: all)")
    ap.add_argument("--url-template", default=URL_TEMPLATE,
                    help="results URL with {n} for the county code (change for other elections)")
    ap.add_argument("--save-html", action="store_true", help="save raw pages to <out>/raw_html")
    ap.add_argument("--from-dir", help="parse saved county_NN.html files instead of downloading")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    out = Path(args.out)
    scraped_at = datetime.now().isoformat(timespec="seconds")
    all_rows = []

    for n in args.counties:
        name = COUNTIES[n]
        url = args.url_template.format(n=n)
        if args.from_dir:
            src = Path(args.from_dir) / f"county_{n:02d}.html"
            html = src.read_text(encoding="utf-8")
        else:
            print(f"[{n:02d}] {name}: {url}")
            html = fetch(url)
            if args.save_html:
                raw = out / "raw_html" / f"county_{n:02d}.html"
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(html, encoding="utf-8")
            time.sleep(args.delay)

        rows = parse_page(html, n, url, scraped_at)
        contests = {(r["contest"], r["district"]) for r in rows}
        print(f"     {len(rows):>5} rows, {len(contests)} contests")
        if not rows:
            print("     WARNING: nothing parsed — page layout may have changed; "
                  "rerun with --save-html and inspect the file.", file=sys.stderr)
        write_csv(out / "by_county" / f"{n:02d}_{slug(name)}.csv", rows)
        all_rows.extend(rows)

    write_csv(out / "md_county_results_all.csv", all_rows)
    print(f"\nDone: {len(all_rows)} rows -> {out / 'md_county_results_all.csv'}")


if __name__ == "__main__":
    main()