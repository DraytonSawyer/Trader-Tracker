"""
House PTR tracker (multi-member)
=================================
Pulls the U.S. House Clerk's annual disclosure ZIP, filters for any number of
configured Members, downloads each new Periodic Transaction Report (PTR),
parses the trades, and writes per-member CSVs + a unified diff.

Source of truth (free, official, no API key):
  https://disclosures-clerk.house.gov/FinancialDisclosure
  ZIP index:  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.ZIP
  PTR PDFs :  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf

Configured for: Pelosi, Khanna, McCaul. Add more by editing MEMBERS below.
For senators, use senate_tracker.py (different filing system).
"""

from __future__ import annotations
import csv
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from pypdf import PdfReader

# ---------- config ----------
MEMBERS = {
    "pelosi":  {"last": "Pelosi",  "first_prefix": "Nancy",   "label": "Nancy Pelosi (D-CA)"},
    "khanna":  {"last": "Khanna",  "first_prefix": "Ro",      "label": "Ro Khanna (D-CA)"},
    "mccaul":  {"last": "McCaul",  "first_prefix": "Michael", "label": "Michael McCaul (R-TX)"},
}

YEAR     = datetime.now().year
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

INDEX_URL = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.ZIP"
PTR_URL   = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
HEADERS   = {"User-Agent": "trader-tracker/1.0 (research) contact@example.com"}


# ---------- step 1: pull the master index (once, shared across all members) ----------
def fetch_filing_index(year: int) -> list[dict]:
    print(f"[1/4] Fetching {year}FD.ZIP from House Clerk...")
    r = requests.get(INDEX_URL.format(year=year), headers=HEADERS, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.endswith(".xml"))
        xml_bytes = zf.read(xml_name)
    root = ET.fromstring(xml_bytes)
    filings = []
    for m in root.findall("Member"):
        filings.append({k: (m.findtext(k) or "").strip() for k in
                        ["Prefix", "Last", "First", "Suffix", "FilingType",
                         "StateDst", "Year", "FilingDate", "DocID"]})
    print(f"      → {len(filings):,} total filings indexed for {year}")
    return filings


# ---------- step 2: filter to one member's PTRs ----------
def filter_member_ptrs(filings: list[dict], last: str, first_prefix: str) -> list[dict]:
    """PTR = FilingType 'P'. Match by last name + first-name prefix to avoid
    false positives when multiple members share a surname (e.g. there are 4
    Smiths in Congress). The first_prefix only needs to disambiguate uniquely."""
    return [f for f in filings
            if f["Last"].lower() == last.lower()
            and f["First"].lower().startswith(first_prefix.lower())
            and f["FilingType"] == "P"]


# ---------- step 3: parse PTR PDFs (unchanged from pelosi_tracker.py) ----------
BLOCK_START   = re.compile(r"(?m)^(SP|JT|DC|OW)\s")
TICKER_RE     = re.compile(r"\(([A-Z][A-Z\.]{0,5})\)\s*\[")
ASSET_CODE_RE = re.compile(r"\[(ST|OP|CS|MF|ET|PE|RS|AB)\]")
TXN_TYPE_RE   = re.compile(r"\b(S \(partial\)|[PSE])\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})")
RANGE_RE      = re.compile(r"\$([\d,]+)\s*-\s*\$([\d,]+)")
DESC_RE       = re.compile(r"D:\s*(.+?)(?=\n(?:F\s|SP\s|JT\s|DC\s|OW\s|\Z))", re.S)

def parse_ptr_text(text: str, doc_id: str) -> list[dict]:
    txns = []
    starts = [m.start() for m in BLOCK_START.finditer(text)] + [len(text)]
    for i in range(len(starts) - 1):
        block = text[starts[i]:starts[i+1]]
        if "Asset Transaction" in block[:60]:
            continue
        owner = block[:2]
        ticker_m = TICKER_RE.search(block)
        code_m   = ASSET_CODE_RE.search(block)
        type_m   = TXN_TYPE_RE.search(block)
        range_m  = RANGE_RE.search(block)
        desc_m   = DESC_RE.search(block)
        asset_match = re.match(r"^(?:SP|JT|DC|OW)\s+([\s\S]+?)\s*\([A-Z\.]+\)\s*\[", block)
        asset = re.sub(r"\s+", " ", asset_match.group(1)).strip() if asset_match else ""
        desc = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else ""
        code = code_m.group(1) if code_m else ""
        is_option = (code == "OP") or "call option" in desc.lower() or "put option" in desc.lower()
        strike_m = re.search(r"strike price of \$([\d,\.]+)", desc, re.I)
        expiry_m = re.search(r"expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", desc, re.I)
        txns.append({
            "doc_id":     doc_id,
            "owner":      owner,
            "asset":      asset[:100],
            "ticker":     ticker_m.group(1) if ticker_m else "",
            "asset_code": code,
            "txn_type":   type_m.group(1) if type_m else "",
            "txn_date":   type_m.group(2) if type_m else "",
            "notif_date": type_m.group(3) if type_m else "",
            "amount_low":  range_m.group(1).replace(",", "") if range_m else "",
            "amount_high": range_m.group(2).replace(",", "") if range_m else "",
            "is_option":  is_option,
            "strike":     strike_m.group(1) if strike_m else "",
            "expiry":     expiry_m.group(1) if expiry_m else "",
            "description": desc[:200],
        })
    return txns

def parse_ptr_pdf(pdf_bytes: bytes, doc_id: str) -> list[dict]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return parse_ptr_text(text, doc_id)


# ---------- step 4: download + parse PTRs for one member ----------
def fetch_and_parse(ptrs: list[dict]) -> list[dict]:
    all_txns = []
    for f in ptrs:
        url = PTR_URL.format(year=f["Year"], doc_id=f["DocID"])
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            txns = parse_ptr_pdf(r.content, f["DocID"])
            for t in txns:
                t["filing_date"] = f["FilingDate"]
            all_txns.extend(txns)
            time.sleep(0.2)  # be polite
        except Exception as e:
            print(f"      ✗ {f['DocID']} failed: {e}")
    return all_txns


# ---------- step 5: snapshot + diff per member ----------
def write_outputs(member_key: str, member_label: str, txns: list[dict]) -> None:
    csv_path  = DATA_DIR / f"{member_key}_trades.csv"
    seen_path = DATA_DIR / f"{member_key}_seen.json"

    def sig(t: dict) -> str:
        return f"{t['doc_id']}|{t['ticker']}|{t['txn_type']}|{t['txn_date']}|{t['amount_low']}"

    prev_seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    new_txns  = [t for t in txns if sig(t) not in prev_seen]

    if txns:
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(txns[0].keys()))
            w.writeheader()
            w.writerows(txns)
    seen_path.write_text(json.dumps([sig(t) for t in txns]))

    print(f"\n  📋 {member_label}: {len(txns)} trades YTD")
    if not prev_seen:
        print(f"     (first run — snapshot saved, no diff)")
    elif new_txns:
        print(f"     🆕 {len(new_txns)} NEW trade(s):")
        for t in new_txns[:15]:  # cap at 15 for readability
            tag = "OPT" if t["is_option"] else "EQ "
            amt = f"${int(t['amount_low']):,}-${int(t['amount_high']):,}" if t["amount_low"] else "?"
            print(f"        {tag} {t['txn_date']:>10} {t['txn_type']:>3} "
                  f"{t['ticker'] or '???':<6} {amt:<22} {t['asset'][:40]}")
        if len(new_txns) > 15:
            print(f"        ... and {len(new_txns) - 15} more (see CSV)")
    else:
        print(f"     no new trades since last run ✓")


# ---------- main ----------
if __name__ == "__main__":
    try:
        index = fetch_filing_index(YEAR)
    except requests.HTTPError as e:
        print(f"HTTP error talking to House Clerk: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n[2/4] Filtering for {len(MEMBERS)} configured members...")
    print(f"[3/4] Downloading + parsing PTR PDFs...")
    print(f"[4/4] Writing outputs + diffing against last run...")

    for key, info in MEMBERS.items():
        ptrs = filter_member_ptrs(index, info["last"], info["first_prefix"])
        if not ptrs:
            print(f"\n  ⚠️  {info['label']}: no PTRs found yet for {YEAR}")
            continue
        txns = fetch_and_parse(ptrs)
        write_outputs(key, info["label"], txns)
