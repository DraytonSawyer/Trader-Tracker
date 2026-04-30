"""Validate the 13F parser against a Burry Q3 2025 fixture."""
from pathlib import Path
from f13_tracker import parse_info_table

xml = (Path(__file__).parent / "test_data" / "burry_q3_2025_infotable.xml").read_bytes()
positions = parse_info_table(xml)

print(f"Parsed {len(positions)} positions\n")
print(f"{'Issuer':<28} {'CUSIP':>10} {'Type':>5} {'Shares':>10}  {'Value':>14}")
print("-" * 80)
total = 0
for p in sorted(positions, key=lambda x: -x["value_usd"]):
    typ = p["put_call"] or "EQ"
    print(f"{p['issuer'][:28]:<28} {p['cusip']:>10} {typ:>5} {p['shares']:>10,}  ${p['value_usd']:>12,}")
    total += p["value_usd"]

print(f"\nTotal disclosed value: ${total:,} (~${total/1e9:.2f}B)")

# Sanity checks
assert len(positions) == 8, f"expected 8 positions, got {len(positions)}"
issuers = {p["issuer"] for p in positions}
assert {"PALANTIR TECHNOLOGIES INC", "NVIDIA CORP", "PFIZER INC", "MOLINA HEALTHCARE INC"} <= issuers
puts = [p for p in positions if p["put_call"] == "Put"]
calls = [p for p in positions if p["put_call"] == "Call"]
equity = [p for p in positions if not p["put_call"]]
assert len(puts) == 2 and len(calls) == 2 and len(equity) == 4
assert total > 1.3e9, f"total value sanity check: {total}"
print(f"\n✓ All assertions passed. {len(puts)} puts, {len(calls)} calls, {len(equity)} equity positions.")
