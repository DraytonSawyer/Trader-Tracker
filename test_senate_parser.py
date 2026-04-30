"""Validate the Senate PTR HTML parser against a realistic fixture."""
from pathlib import Path
from senate_tracker import parse_ptr_html

html = (Path(__file__).parent / "test_data" / "tuberville_ptr_sample.html").read_text()
txns = parse_ptr_html(html, "test-uuid-1234")

print(f"Parsed {len(txns)} transactions\n")
print(f"{'Date':>12}  {'Owner':>8}  {'Ticker':>6}  {'Type':>15}  {'Amount':>22}  Asset")
print("-" * 100)
for t in txns:
    print(f"{t['txn_date']:>12}  {t['owner']:>8}  {(t['ticker'] or '???'):>6}  "
          f"{t['txn_type']:>15}  {t['amount']:>22}  {t['asset'][:35]}")

# Sanity checks
assert len(txns) == 6, f"expected 6, got {len(txns)}"
tickers = {t["ticker"] for t in txns if t["ticker"]}
assert {"NVDA", "MSFT", "AAPL", "LMT", "XOM"} == tickers, f"got: {tickers}"
owners = {t["owner"] for t in txns}
assert {"Self", "Spouse"} <= owners
sales = [t for t in txns if "Sale" in t["txn_type"]]
buys  = [t for t in txns if t["txn_type"] == "Purchase"]
assert len(sales) == 1 and len(buys) == 5
print(f"\n✓ All assertions passed. {len(buys)} buys, {len(sales)} sale(s), {len(tickers)} unique tickers.")
