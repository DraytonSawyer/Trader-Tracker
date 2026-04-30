"""
13F tracker
===========
Pulls the latest 13F-HR filing from SEC EDGAR for any hedge fund by CIK,
parses the XML information table, and diffs against the last snapshot.

Source of truth: data.sec.gov (free, no auth, JSON + XML).

Currently configured for:
  - Bill Ackman / Pershing Square     (CIK 0001336528)
  - Michael Burry  / Scion Asset Mgmt (CIK 0001649339)
  - Leopold Aschenbrenner / Sit. Aw.  (CIK 0002045724)

Adding a new fund is a one-line change to FUNDS below.

13F mechanics:
  - Filed quarterly, within 45 days of quarter end.
  - Discloses LONG U.S. equity + listed options only.
  - Shows VALUE (in $1000s, as of quarter-end) and SSH PRNAMT (share count).
  - Options reported as PUT / CALL with notional value, not premium paid.
  - Short positions are NOT disclosed.
  - International holdings are NOT disclosed.

Run via: python f13_tracker.py
First run snapshots everything. Subsequent runs print the diff.
"""

from __future__ import annotations
import csv
import json
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

# ---------- config ----------
FUNDS = {
    "berkshire":      {"cik": "0001067983", "label": "Berkshire Hathaway (Buffett)"},
    "ackman":         {"cik": "0001336528", "label": "Pershing Square (Ackman)"},
    "klarman":        {"cik": "0001061768", "label": "Baupost Group (Klarman)"},
    "einhorn":        {"cik": "0001489933", "label": "DME Capital (Einhorn)"},
    "druckenmiller":  {"cik": "0001536411", "label": "Duquesne Family Office (Druckenmiller)"},
    "aschenbrenner":  {"cik": "0002045724", "label": "Situational Awareness (Aschenbrenner)"},
}

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# SEC requires an identifying User-Agent (their guidance: include real contact info)
HEADERS = {
    "User-Agent": "trader-tracker research-bot contact@example.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_BASE   = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}"


# ---------- step 1: find the latest 13F-HR filing ----------
def latest_13f(cik: str) -> dict:
    """Return {accession, filing_date, period} for the most recent 13F-HR."""
    r = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=HEADERS, timeout=30)
    r.raise_for_status()
    sub = r.json()
    recent = sub["filings"]["recent"]

    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            return {
                "accession":   recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "period":      recent["reportDate"][i],
                "cik_int":     int(cik),
            }
    raise RuntimeError(f"No 13F-HR found for CIK {cik}")


# ---------- step 2: locate + fetch the information table XML ----------
def fetch_info_table(filing: dict) -> bytes:
    """Find and download the infoTable XML inside the filing's archive folder."""
    accession_nodash = filing["accession"].replace("-", "")
    base = ARCHIVES_BASE.format(cik_int=filing["cik_int"], accession_nodash=accession_nodash)

    # Pull the index.json for this filing's folder to find the right XML
    idx_url = f"{base}/index.json"
    idx_headers = {**HEADERS, "Host": "www.sec.gov"}  # Archives lives on www., not data.
    r = requests.get(idx_url, headers=idx_headers, timeout=30)
    r.raise_for_status()
    items = r.json()["directory"]["item"]

    # The information table is the XML that's NOT primary_doc.xml
    info_table_name = next(
        (it["name"] for it in items
         if it["name"].endswith(".xml") and it["name"] != "primary_doc.xml"),
        None
    )
    if not info_table_name:
        raise RuntimeError(f"No info table XML found in {base}")

    time.sleep(0.15)  # SEC asks for <10 req/sec; we're well under
    r = requests.get(f"{base}/{info_table_name}", headers=idx_headers, timeout=30)
    r.raise_for_status()
    return r.content


# ---------- step 3: parse the XML into a clean list of positions ----------
NS_RE = re.compile(r"\{[^}]+\}")

def strip_ns(tag: str) -> str:
    return NS_RE.sub("", tag)

def parse_info_table(xml_bytes: bytes) -> list[dict]:
    """Each <infoTable> element is one position. Return a normalized list."""
    root = ET.fromstring(xml_bytes)
    rows = []
    for it in root.iter():
        if strip_ns(it.tag) != "infoTable":
            continue
        record = {}
        for child in it:
            tag = strip_ns(child.tag)
            if tag == "shrsOrPrnAmt":
                for gc in child:
                    record[strip_ns(gc.tag)] = (gc.text or "").strip()
            elif tag == "votingAuthority":
                continue  # not useful for tracking
            else:
                record[tag] = (child.text or "").strip()
        rows.append({
            "issuer":     record.get("nameOfIssuer", ""),
            "class":      record.get("titleOfClass", ""),
            "cusip":      record.get("cusip", ""),
            "value_usd":  int(record.get("value", "0") or 0) * 1000,  # SEC reports in $1000s
            "shares":     int(record.get("sshPrnamt", "0") or 0),
            "share_type": record.get("sshPrnamtType", ""),
            "put_call":   record.get("putCall", ""),  # "" / "Put" / "Call"
        })
    return rows


# ---------- step 4: snapshot + diff ----------
def position_key(p: dict) -> str:
    """Unique key per position (CUSIP + put/call distinguishes equity from options)."""
    return f"{p['cusip']}|{p['put_call'] or 'EQ'}"

def write_outputs(fund_key: str, fund_label: str, filing: dict, positions: list[dict]) -> None:
    csv_path  = DATA_DIR / f"{fund_key}_holdings.csv"
    snap_path = DATA_DIR / f"{fund_key}_last.json"

    # Write the current snapshot CSV
    if positions:
        positions_sorted = sorted(positions, key=lambda p: -p["value_usd"])
        total_value = sum(p["value_usd"] for p in positions_sorted)
        for p in positions_sorted:
            p["pct_portfolio"] = round(100 * p["value_usd"] / total_value, 2) if total_value else 0
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(positions_sorted[0].keys()))
            w.writeheader()
            w.writerows(positions_sorted)

    # Diff against last snapshot
    prev = json.loads(snap_path.read_text()) if snap_path.exists() else None
    snapshot = {
        "filing":    filing,
        "positions": {position_key(p): p for p in positions},
    }

    print(f"\n{'='*70}\n  {fund_label}")
    print(f"  Filing: {filing['accession']} · period {filing['period']} · filed {filing['filing_date']}")
    print(f"  {len(positions)} positions, ${sum(p['value_usd'] for p in positions)/1e9:.2f}B disclosed value\n{'='*70}")

    if not prev:
        print("  First run — no diff. Top 5 positions:")
        for p in sorted(positions, key=lambda p: -p["value_usd"])[:5]:
            opt = f" [{p['put_call']}]" if p['put_call'] else ""
            print(f"    {p['issuer'][:35]:<35} ${p['value_usd']/1e6:>9,.1f}M  ({p.get('pct_portfolio', 0)}%){opt}")
    elif prev["filing"]["accession"] == filing["accession"]:
        print(f"  No new filing since last run (still {filing['period']}) ✓")
    else:
        prev_pos = prev["positions"]
        curr_pos = snapshot["positions"]
        added    = [k for k in curr_pos if k not in prev_pos]
        exited   = [k for k in prev_pos if k not in curr_pos]
        kept     = [k for k in curr_pos if k in prev_pos]

        increased = [k for k in kept if curr_pos[k]["shares"] > prev_pos[k]["shares"] * 1.05]
        decreased = [k for k in kept if curr_pos[k]["shares"] < prev_pos[k]["shares"] * 0.95]

        print(f"  🆕 NEW filing detected (was {prev['filing']['period']}, now {filing['period']})\n")
        print(f"     ➕ NEW positions ({len(added)}):")
        for k in sorted(added, key=lambda k: -curr_pos[k]["value_usd"])[:10]:
            p = curr_pos[k]
            opt = f" [{p['put_call']}]" if p['put_call'] else ""
            print(f"        {p['issuer'][:35]:<35} ${p['value_usd']/1e6:>9,.1f}M{opt}")
        print(f"\n     ❌ EXITED positions ({len(exited)}):")
        for k in sorted(exited, key=lambda k: -prev_pos[k]["value_usd"])[:10]:
            p = prev_pos[k]
            opt = f" [{p['put_call']}]" if p['put_call'] else ""
            print(f"        {p['issuer'][:35]:<35} was ${p['value_usd']/1e6:>9,.1f}M{opt}")
        print(f"\n     📈 INCREASED ({len(increased)}):")
        for k in sorted(increased, key=lambda k: -(curr_pos[k]['shares'] - prev_pos[k]['shares']))[:10]:
            p, q = curr_pos[k], prev_pos[k]
            chg = (p['shares'] - q['shares']) / q['shares'] * 100 if q['shares'] else 0
            print(f"        {p['issuer'][:35]:<35} +{chg:>5.0f}%  → ${p['value_usd']/1e6:,.1f}M")
        print(f"\n     📉 DECREASED ({len(decreased)}):")
        for k in sorted(decreased, key=lambda k: (curr_pos[k]['shares'] - prev_pos[k]['shares']))[:10]:
            p, q = curr_pos[k], prev_pos[k]
            chg = (p['shares'] - q['shares']) / q['shares'] * 100 if q['shares'] else 0
            print(f"        {p['issuer'][:35]:<35} {chg:>+5.0f}%  → ${p['value_usd']/1e6:,.1f}M")

    snap_path.write_text(json.dumps(snapshot, indent=2))


# ---------- main ----------
def track(fund_key: str, fund_label: str, cik: str) -> None:
    try:
        filing = latest_13f(cik)
        time.sleep(0.15)
        xml = fetch_info_table(filing)
        positions = parse_info_table(xml)
        write_outputs(fund_key, fund_label, filing, positions)
    except Exception as e:
        print(f"\n[{fund_label}] FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    for key, info in FUNDS.items():
        track(key, info["label"], info["cik"])
        time.sleep(0.3)  # be polite to SEC
