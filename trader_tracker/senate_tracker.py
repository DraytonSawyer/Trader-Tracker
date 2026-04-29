"""
Senate PTR tracker (multi-senator)
===================================
Scrapes Periodic Transaction Reports from the U.S. Senate's efdsearch.senate.gov
for any number of configured senators. Diffs against last run.

Source: https://efdsearch.senate.gov/search/

This site requires:
  1. A click-through "I understand" agreement (POSTed once per session)
  2. A CSRF token on every POST (Django app)
  3. A session cookie that gets set after agreement is accepted

Configured for: Tuberville (R-AL), Mullin (R-OK), Whitehouse (D-RI).
Add more by editing SENATORS below.

LEGAL NOTE: Per § 105(c) of the Ethics in Government Act, this data may NOT be
used for commercial purposes (other than news media). This script is for
personal research. Don't use it to power a paid service.
"""

from __future__ import annotations
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- config ----------
SENATORS = {
    "tuberville": {"first": "Tommy",    "last": "Tuberville", "label": "Tommy Tuberville (R-AL)"},
    "mullin":     {"first": "Markwayne","last": "Mullin",     "label": "Markwayne Mullin (R-OK)"},
    "whitehouse": {"first": "Sheldon",  "last": "Whitehouse", "label": "Sheldon Whitehouse (D-RI)"},
}

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BASE       = "https://efdsearch.senate.gov"
HOME_URL   = f"{BASE}/search/home/"
SEARCH_URL = f"{BASE}/search/"
DATA_URL   = f"{BASE}/search/report/data/"
PTR_URL    = f"{BASE}/search/view/ptr/{{uuid}}/"

# Browser-like UA — Senate site rejects default requests UA.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- step 1: accept the click-through agreement ----------
def authenticated_session() -> requests.Session:
    """Returns a Session with cookies set such that searches will succeed."""
    s = requests.Session()
    s.headers.update(HEADERS)

    # GET home to receive the CSRF cookie + scrape the form's csrfmiddlewaretoken
    r = s.get(HOME_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not token_input:
        raise RuntimeError("Couldn't find CSRF token on home page — site layout may have changed.")
    csrf = token_input["value"]

    # POST the agreement
    r = s.post(HOME_URL,
               data={"csrfmiddlewaretoken": csrf, "prohibition_agreement": "1"},
               headers={"Referer": HOME_URL},
               timeout=30)
    r.raise_for_status()

    # After agreement, GET /search/ to confirm and refresh CSRF for subsequent POSTs
    r = s.get(SEARCH_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if token_input:
        s.headers["X-CSRFToken"] = token_input["value"]
    elif "csrftoken" in s.cookies:
        s.headers["X-CSRFToken"] = s.cookies["csrftoken"]

    return s


# ---------- step 2: search for one senator's PTR filings ----------
def search_senator_ptrs(s: requests.Session, first: str, last: str) -> list[dict]:
    """Returns list of {filing_date, uuid, label} dicts for this senator's PTRs."""
    payload = {
        "csrfmiddlewaretoken": s.headers.get("X-CSRFToken", ""),
        "first_name":   first,
        "last_name":    last,
        # filer_types: senators=1, candidates=4. We want senators.
        "filer_types":  ["1"],
        # report_types: 11 = Periodic Transaction Report
        "report_types": ["11"],
        "submitted_start_date": "01/01/2023 00:00:00",
        "submitted_end_date":   datetime.now().strftime("%m/%d/%Y 23:59:59"),
        "candidate_state":      "",
        "senator_state":        "",
        "office_id":            "",
        "start":                "0",
        "length":               "100",
    }
    r = s.post(DATA_URL, data=payload,
               headers={"Referer": SEARCH_URL,
                        "X-Requested-With": "XMLHttpRequest"},
               timeout=30)
    r.raise_for_status()
    rows = r.json().get("data", [])

    results = []
    for row in rows:
        # Each row is [first, last, link_html, type, filing_date]
        # link_html looks like: <a href="/search/view/ptr/UUID/">Periodic Transaction Report</a>
        link_match = re.search(r"/search/view/ptr/([0-9a-f-]+)/", row[2])
        if not link_match:
            continue
        results.append({
            "uuid":        link_match.group(1),
            "filing_date": row[4].strip() if len(row) > 4 else "",
            "label":       BeautifulSoup(row[2], "html.parser").get_text(strip=True),
        })
    return results


# ---------- step 3: parse each PTR's HTML table ----------
def parse_ptr_html(html: str, uuid: str) -> list[dict]:
    """Each PTR is a single HTML page with a <table> of transactions."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    txns = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 7:
            continue
        # Standard PTR columns:
        # # | Transaction Date | Owner | Ticker | Asset Name | Asset Type | Type | Amount | Comment
        try:
            txns.append({
                "uuid":        uuid,
                "txn_date":    cells[1],
                "owner":       cells[2],          # Self / Spouse / Joint / Dependent Child
                "ticker":      cells[3].replace("--", "").strip(),
                "asset":       cells[4][:120],
                "asset_type":  cells[5],
                "txn_type":    cells[6],          # Purchase / Sale / Exchange
                "amount":      cells[7] if len(cells) > 7 else "",
                "comment":     cells[8] if len(cells) > 8 else "",
            })
        except IndexError:
            continue
    return txns


def fetch_and_parse_ptrs(s: requests.Session, ptrs: list[dict]) -> list[dict]:
    all_txns = []
    for p in ptrs:
        try:
            r = s.get(PTR_URL.format(uuid=p["uuid"]),
                      headers={"Referer": SEARCH_URL}, timeout=30)
            r.raise_for_status()
            txns = parse_ptr_html(r.text, p["uuid"])
            for t in txns:
                t["filing_date"] = p["filing_date"]
            all_txns.extend(txns)
            time.sleep(0.3)  # be polite — Senate site is rate-limited
        except Exception as e:
            print(f"      ✗ PTR {p['uuid'][:8]} failed: {e}")
    return all_txns


# ---------- step 4: snapshot + diff per senator ----------
def write_outputs(senator_key: str, label: str, txns: list[dict]) -> None:
    csv_path  = DATA_DIR / f"{senator_key}_trades.csv"
    seen_path = DATA_DIR / f"{senator_key}_seen.json"

    def sig(t: dict) -> str:
        return f"{t['uuid']}|{t['ticker']}|{t['txn_type']}|{t['txn_date']}|{t['amount']}"

    prev_seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    new_txns  = [t for t in txns if sig(t) not in prev_seen]

    if txns:
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(txns[0].keys()))
            w.writeheader()
            w.writerows(txns)
    seen_path.write_text(json.dumps([sig(t) for t in txns]))

    print(f"\n  📋 {label}: {len(txns)} trades from disclosed PTRs")
    if not prev_seen:
        print(f"     (first run — snapshot saved)")
    elif new_txns:
        print(f"     🆕 {len(new_txns)} NEW trade(s):")
        for t in new_txns[:15]:
            print(f"        {t['txn_date']:>12}  {t['txn_type']:>10}  "
                  f"{(t['ticker'] or '???'):<6}  {t['amount']:<24}  {t['asset'][:40]}")
        if len(new_txns) > 15:
            print(f"        ... and {len(new_txns) - 15} more (see CSV)")
    else:
        print(f"     no new trades since last run ✓")


# ---------- main ----------
if __name__ == "__main__":
    print("[1/4] Authenticating with efdsearch.senate.gov...")
    try:
        session = authenticated_session()
    except Exception as e:
        print(f"Auth failed: {e}", file=sys.stderr)
        sys.exit(1)

    for key, info in SENATORS.items():
        print(f"\n[2-4] Processing {info['label']}...")
        try:
            ptrs = search_senator_ptrs(session, info["first"], info["last"])
            print(f"      Found {len(ptrs)} PTR filing(s) since 2023")
            if not ptrs:
                continue
            txns = fetch_and_parse_ptrs(session, ptrs)
            write_outputs(key, info["label"], txns)
        except Exception as e:
            print(f"  ✗ {info['label']} failed: {e}")
        time.sleep(1.0)  # extra politeness between senators
