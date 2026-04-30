"""Validate the PTR parser against a real Pelosi filing (Filing ID 20033725, Jan 2026)."""
from pathlib import Path
from house_tracker import parse_ptr_text

text = (Path(__file__).parent / "test_data" / "ptr_20033725.txt").read_text()
txns = parse_ptr_text(text, "20033725")

print(f"Parsed {len(txns)} transactions\n")
print(f"{'Owner':>5} {'Ticker':>7} {'Type':>10} {'Date':>11}  {'Amount':>22}  {'Opt?':>4}  Asset")
print("-" * 110)
for t in txns:
    amt = f"${int(t['amount_low']):,}-${int(t['amount_high']):,}" if t["amount_low"] else "—"
    opt = "✓" if t["is_option"] else ""
    print(f"{t['owner']:>5} {t['ticker']:>7} {t['txn_type']:>10} {t['txn_date']:>11}  {amt:>22}  {opt:>4}  {t['asset'][:40]}")

# Sanity checks
assert len(txns) == 18, f"expected 18 txns, got {len(txns)}"
tickers = {t["ticker"] for t in txns}
assert {"AB", "GOOGL", "AMZN", "AAPL", "NVDA", "PYPL", "TEM", "VSNT", "VST", "DIS"} <= tickers, f"missing tickers: {tickers}"
opt_count = sum(1 for t in txns if t["is_option"])
assert opt_count >= 4, f"expected at least 4 option trades, got {opt_count}"
print(f"\n✓ All assertions passed. {opt_count} option trades detected.")
