"""
Static site builder
===================
Reads everything in data/ (output from the three trackers) and writes a single
self-contained index.html to docs/. GitHub Pages serves docs/ automatically.

Run AFTER the trackers complete. The GitHub Actions workflow does this in order:
  python house_tracker.py
  python senate_tracker.py
  python f13_tracker.py
  python build_site.py    # this script

No external dependencies — pure stdlib. The output is one HTML file with all
data embedded as JSON, all CSS and JS inline, and zero runtime requirements.
"""

from __future__ import annotations
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_DIR.mkdir(exist_ok=True)

# ---------- trader metadata (display config) ----------
# These are the labels, colors, and theses we want shown for each tracked entity.
# The actual position data is loaded from the CSVs.
TRADERS = {
    # Hedge funds (13F)
    "berkshire":     {"display": "Warren Buffett",         "fund": "Berkshire Hathaway",      "kind": "fund",   "av_class": "av-buf",  "initials": "WB", "color": "#854F0B", "thesis": "Quality compounders, financials, energy. Holding cash near record levels."},
    "ackman":        {"display": "Bill Ackman",            "fund": "Pershing Square",         "kind": "fund",   "av_class": "av-ack",  "initials": "BA", "color": "#993556", "thesis": "Concentrated quality compounders: infrastructure + platform tech."},
    "klarman":       {"display": "Seth Klarman",           "fund": "Baupost Group",           "kind": "fund",   "av_class": "av-klar", "initials": "SK", "color": "#5F5E5A", "thesis": "Deep value, event-driven, willing to hold cash."},
    "einhorn":       {"display": "David Einhorn",          "fund": "DME Capital",             "kind": "fund",   "av_class": "av-ein",  "initials": "DE", "color": "#A32D2D", "thesis": "Long/short value, willing to short publicly. Cyclicals, special situations.", "badge": "Long/short"},
    "druckenmiller": {"display": "Stanley Druckenmiller",  "fund": "Duquesne Family Office",  "kind": "fund",   "av_class": "av-druk", "initials": "SD", "color": "#185FA5", "thesis": "Macro rotation: financials, Brazil, equal-weight S&P. Healthcare core.", "badge": "Family office"},
    "aschenbrenner": {"display": "Leopold Aschenbrenner",  "fund": "Situational Awareness",   "kind": "fund",   "av_class": "av-asch", "initials": "LA", "color": "#534AB7", "thesis": "AI picks-and-shovels: power, GPU cloud, optical, miners pivoting to hosting."},
    # House
    "pelosi":        {"display": "Nancy Pelosi",           "fund": "Rep · CA-11",             "kind": "house",  "av_class": "av-pelo", "initials": "NP", "party": "D", "leaving": "2027"},
    "khanna":        {"display": "Ro Khanna",              "fund": "Rep · CA-17",             "kind": "house",  "av_class": "av-kha",  "initials": "RK", "party": "D"},
    "mccaul":        {"display": "Michael McCaul",         "fund": "Rep · TX-10",             "kind": "house",  "av_class": "av-mcc",  "initials": "MM", "party": "R"},
    # Senate
    "tuberville":    {"display": "Tommy Tuberville",       "fund": "Sen · AL",                "kind": "senate", "av_class": "av-tub",  "initials": "TT", "party": "R", "leaving": "2027"},
    "mullin":        {"display": "Markwayne Mullin",       "fund": "Sen · OK",                "kind": "senate", "av_class": "av-mul",  "initials": "MK", "party": "R"},
    "whitehouse":    {"display": "Sheldon Whitehouse",     "fund": "Sen · RI",                "kind": "senate", "av_class": "av-whi",  "initials": "SW", "party": "D"},
}


# ---------- load each trader's data from CSV ----------
def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as fh:
        return list(csv.DictReader(fh))


def load_fund_holdings(key: str) -> dict:
    """13F output → top positions sorted by value."""
    rows = load_csv(DATA_DIR / f"{key}_holdings.csv")
    if not rows:
        return {"top": [], "total_value": 0, "holdings_count": 0}
    for r in rows:
        r["value_usd"] = int(r.get("value_usd", 0) or 0)
        r["pct_portfolio"] = float(r.get("pct_portfolio", 0) or 0)
    rows.sort(key=lambda r: -r["value_usd"])
    total = sum(r["value_usd"] for r in rows)
    return {
        "top": rows[:5],
        "total_value": total,
        "holdings_count": len(rows),
    }


def load_politician_trades(key: str) -> dict:
    """House/Senate output → recent trades + summary stats."""
    rows = load_csv(DATA_DIR / f"{key}_trades.csv")
    if not rows:
        return {"recent": [], "trade_count": 0, "tickers": []}
    # Sort by transaction date if present (House schema: 'txn_date', Senate same)
    def parse_date(s: str):
        try:
            return datetime.strptime(s, "%m/%d/%Y")
        except (ValueError, TypeError):
            return datetime.min
    rows.sort(key=lambda r: parse_date(r.get("txn_date", "")), reverse=True)
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    return {
        "recent": rows[:10],
        "trade_count": len(rows),
        "tickers": tickers,
    }


# ---------- aggregate consensus and recent activity ----------
def build_consensus(all_data: dict) -> list[dict]:
    """Find tickers held by 2+ position-based traders. Skip flow traders."""
    POSITION_TRADERS = ["berkshire", "ackman", "klarman", "einhorn",
                        "druckenmiller", "aschenbrenner", "pelosi", "whitehouse"]
    holdings_by_ticker = {}
    for key in POSITION_TRADERS:
        d = all_data.get(key, {})
        positions = d.get("top", []) or [{"ticker": t} for t in d.get("tickers", [])[:5]]
        for p in positions:
            tk = p.get("ticker", "").strip()
            if not tk:
                continue
            holdings_by_ticker.setdefault(tk, []).append(key)
    consensus = [
        {"ticker": tk, "longs": holders}
        for tk, holders in holdings_by_ticker.items()
        if len(holders) >= 2
    ]
    consensus.sort(key=lambda c: -len(c["longs"]))
    return consensus[:10]


def build_recent_feed(all_data: dict) -> list[dict]:
    """Combine recent trades from politicians + recent fund moves into one feed."""
    feed = []
    for key, d in all_data.items():
        if key not in TRADERS:  # skip synthetic keys like __consensus__
            continue
        meta = TRADERS[key]
        for r in d.get("recent", [])[:5]:
            feed.append({
                "who": key,
                "who_initials": meta["initials"],
                "who_av_class": meta["av_class"],
                "date": r.get("txn_date", ""),
                "ticker": r.get("ticker", "???"),
                "action": r.get("txn_type", ""),
                "amount": f"${r.get('amount_low', '')}-${r.get('amount_high', '')}" if r.get("amount_low") else r.get("amount", ""),
                "asset": r.get("asset", "")[:60],
            })
    # Sort by date desc — best effort
    def parse(d):
        try: return datetime.strptime(d, "%m/%d/%Y")
        except: return datetime.min
    feed.sort(key=lambda e: parse(e["date"]), reverse=True)
    return feed[:20]


# ---------- HTML template ----------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trader Tracker</title>
  <style>
    :root {
      --bg: #FAFAF7;
      --surface: #FFFFFF;
      --surface-alt: #F1EFE8;
      --text: #1A1A19;
      --text-secondary: #5F5E5A;
      --text-tertiary: #888780;
      --border: rgba(0,0,0,0.08);
      --border-strong: rgba(0,0,0,0.15);
      --info-bg: #E6F1FB;
      --info-text: #0C447C;
      --success-bg: #EAF3DE;
      --success-text: #27500A;
      --warning-bg: #FAEEDA;
      --warning-text: #633806;
      --danger-bg: #FCEBEB;
      --danger-text: #791F1F;
      --radius-md: 8px;
      --radius-lg: 12px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #1A1A19;
        --surface: #242422;
        --surface-alt: #2C2C2A;
        --text: #FAFAF7;
        --text-secondary: #B4B2A9;
        --text-tertiary: #888780;
        --border: rgba(255,255,255,0.1);
        --border-strong: rgba(255,255,255,0.2);
      }
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      font-size: 16px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
    .container { max-width: 760px; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
    h1 { font-size: 22px; font-weight: 500; margin: 0 0 4px; }
    .updated { font-size: 12px; color: var(--text-tertiary); margin-bottom: 1.5rem; }
    .tabs { display: flex; gap: 4px; margin-bottom: 1rem; border-bottom: 1px solid var(--border);
      overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .tab { font-size: 13px; padding: 8px 12px; border: none; background: transparent;
      color: var(--text-secondary); cursor: pointer; border-bottom: 2px solid transparent;
      white-space: nowrap; }
    .tab.active { color: var(--text); border-bottom-color: var(--info-text); font-weight: 500; }
    .tcard { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      padding: 14px 16px; margin-bottom: 10px; }
    .row-head { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
    .avatar { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center;
      justify-content: center; font-weight: 500; font-size: 12px; flex-shrink: 0; }
    .av-buf { background: #FAEEDA; color: #633806; }
    .av-asch { background: #EEEDFE; color: #3C3489; }
    .av-pelo, .av-kha { background: #E1F5EE; color: #085041; }
    .av-ack { background: #FBEAF0; color: #72243E; }
    .av-druk, .av-whi { background: #E6F1FB; color: #0C447C; }
    .av-klar { background: #F1EFE8; color: #444441; }
    .av-ein, .av-tub { background: #FCEBEB; color: #791F1F; }
    .av-mcc, .av-mul { background: #FAEEDA; color: #633806; }
    .name { font-weight: 500; font-size: 14px; margin: 0; }
    .sub { font-size: 12px; color: var(--text-secondary); margin: 0; }
    .thesis { font-size: 12px; color: var(--text-secondary); margin: 8px 0 0; }
    .meta-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin: 12px 0; }
    .meta { background: var(--surface-alt); padding: 8px 10px; border-radius: var(--radius-md); }
    .meta-label { font-size: 11px; color: var(--text-secondary); }
    .meta-val { font-size: 14px; font-weight: 500; margin-top: 2px; }
    .pos-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0;
      font-size: 13px; border-top: 1px solid var(--border); }
    .pos-row:first-of-type { border-top: none; }
    .pos-left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
    .pos-right { display: flex; align-items: center; gap: 8px; }
    .ticker { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-weight: 500; min-width: 60px; }
    .asset-name { color: var(--text-secondary); font-size: 12px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; }
    .bar-bg { background: var(--surface-alt); height: 6px; border-radius: 999px;
      overflow: hidden; width: 60px; }
    .bar-fill { height: 100%; border-radius: 999px; }
    .pct { font-variant-numeric: tabular-nums; min-width: 40px; text-align: right; font-size: 12px; }
    .badge { font-size: 10px; padding: 2px 7px; border-radius: 4px; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.04em; }
    .badge-d { background: var(--success-bg); color: var(--success-text); }
    .badge-r { background: var(--danger-bg); color: var(--danger-text); }
    .badge-info { background: var(--info-bg); color: var(--info-text); }
    .badge-warn { background: var(--warning-bg); color: var(--warning-text); }
    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; display: inline-block;
      font-weight: 500; }
    .p-buy { background: var(--success-bg); color: var(--success-text); }
    .p-sell { background: var(--danger-bg); color: var(--danger-text); }
    .p-new { background: var(--info-bg); color: var(--info-text); }
    .empty { text-align: center; padding: 2rem 1rem; color: var(--text-tertiary); font-size: 13px; }
    .footer { font-size: 11px; color: var(--text-tertiary); margin-top: 2rem; line-height: 1.6; }
    .footer a { color: var(--text-secondary); }
  </style>
</head>
<body>
  <div class="container">
    <h1>Trader tracker</h1>
    <div class="updated">__TRADER_COUNT__ traders · last updated __UPDATED__ UTC</div>

    <div class="tabs" role="tablist">
      <button class="tab active" data-tab="funds">Funds</button>
      <button class="tab" data-tab="house">House</button>
      <button class="tab" data-tab="senate">Senate</button>
      <button class="tab" data-tab="consensus">Consensus</button>
      <button class="tab" data-tab="recent">Recent</button>
    </div>

    <div id="view-funds" class="view"></div>
    <div id="view-house" class="view" hidden></div>
    <div id="view-senate" class="view" hidden></div>
    <div id="view-consensus" class="view" hidden></div>
    <div id="view-recent" class="view" hidden></div>

    <div class="footer">
      Built from public disclosures: SEC EDGAR (13F-HR), House Clerk PTRs,
      Senate efdsearch PTRs. Personal research only — Senate disclosures may not be
      used for commercial purposes per § 105(c) of the Ethics in Government Act.
      <br><br>
      Source: <a href="__REPO_URL__">github.com/.../trader-tracker</a>
    </div>
  </div>

<script>
const DATA = __DATA_JSON__;
const META = __META_JSON__;

function fmtMoney(v) {
  if (v >= 1e9) return "$" + (v/1e9).toFixed(2) + "B";
  if (v >= 1e6) return "$" + (v/1e6).toFixed(0) + "M";
  if (v >= 1e3) return "$" + (v/1e3).toFixed(0) + "K";
  return "$" + v;
}

function actPill(action) {
  const a = (action || "").toLowerCase();
  if (a.includes("purchase") || a === "p") return '<span class="pill p-buy">Buy</span>';
  if (a.includes("sale") || a === "s") return '<span class="pill p-sell">Sell</span>';
  if (a.includes("partial")) return '<span class="pill p-sell">Partial</span>';
  if (a.includes("exchange") || a === "e") return '<span class="pill p-new">Exchange</span>';
  return '';
}

function avatarFor(key) {
  const m = META[key];
  if (!m) return '';
  return '<div class="avatar ' + m.av_class + '" style="width:22px;height:22px;font-size:10px;" title="' + m.display + '">' + m.initials + '</div>';
}

function renderEmpty(msg) {
  return '<div class="empty">' + msg + '</div>';
}

function renderFund(key) {
  const m = META[key];
  const d = DATA[key] || {};
  const top = d.top || [];
  if (top.length === 0) {
    return '<div class="tcard">' + headerHtml(m) + renderEmpty("No filing data yet — first run pending.") + '</div>';
  }
  let html = '<div class="tcard">' + headerHtml(m);
  html += '<p class="thesis">' + (m.thesis || '') + '</p>';
  html += '<div class="meta-row">';
  html += '<div class="meta"><div class="meta-label">Disclosed</div><div class="meta-val">' + fmtMoney(d.total_value || 0) + '</div></div>';
  html += '<div class="meta"><div class="meta-label">Holdings</div><div class="meta-val">' + (d.holdings_count || 0) + '</div></div>';
  html += '<div class="meta"><div class="meta-label">Top 5</div><div class="meta-val">' + (top.slice(0,5).reduce((s,p) => s + (parseFloat(p.pct_portfolio)||0), 0)).toFixed(0) + '%</div></div>';
  html += '</div>';
  for (const p of top) {
    const pct = parseFloat(p.pct_portfolio) || 0;
    const opt = p.put_call ? ' <span class="badge badge-info">' + p.put_call + '</span>' : '';
    html += '<div class="pos-row">';
    html += '<div class="pos-left"><span class="ticker">' + (p.ticker || p.cusip || '?') + '</span>';
    html += '<span class="asset-name">' + (p.issuer || '') + opt + '</span></div>';
    html += '<div class="pos-right">';
    html += '<div class="bar-bg"><div class="bar-fill" style="width:' + Math.min(100, pct*3) + '%;background:' + m.color + ';"></div></div>';
    html += '<span class="pct">' + pct.toFixed(1) + '%</span></div></div>';
  }
  html += '</div>';
  return html;
}

function headerHtml(m) {
  let badges = '';
  if (m.party) badges += ' <span class="badge badge-' + m.party.toLowerCase() + '">' + m.party + '</span>';
  if (m.badge) badges += ' <span class="badge badge-info">' + m.badge + '</span>';
  if (m.leaving) badges += ' <span class="badge badge-warn">Leaves ' + m.leaving + '</span>';
  return '<div class="row-head"><div class="avatar ' + m.av_class + '">' + m.initials + '</div>' +
         '<div style="flex:1;min-width:0;"><p class="name">' + m.display + badges + '</p>' +
         '<p class="sub">' + m.fund + '</p></div></div>';
}

function renderPolitician(key) {
  const m = META[key];
  const d = DATA[key] || {};
  const recent = d.recent || [];
  let html = '<div class="tcard">' + headerHtml(m);
  if (recent.length === 0) {
    html += renderEmpty("No trades yet — first run pending.");
    html += '</div>';
    return html;
  }
  html += '<div class="meta-row" style="grid-template-columns:1fr 1fr;">';
  html += '<div class="meta"><div class="meta-label">Trades YTD</div><div class="meta-val">' + d.trade_count + '</div></div>';
  html += '<div class="meta"><div class="meta-label">Unique tickers</div><div class="meta-val">' + d.tickers.length + '</div></div>';
  html += '</div>';
  html += '<p class="sub" style="margin:12px 0 6px;text-transform:uppercase;letter-spacing:0.05em;font-size:11px;color:var(--text-tertiary);">Recent trades</p>';
  for (const r of recent) {
    html += '<div class="pos-row">';
    html += '<div class="pos-left"><span class="ticker">' + (r.ticker || '???') + '</span>';
    html += '<span class="asset-name">' + (r.asset || '') + '</span></div>';
    html += '<div class="pos-right">' + actPill(r.txn_type) + '<span class="pct" style="min-width:80px;">' + (r.txn_date || '') + '</span></div></div>';
  }
  html += '</div>';
  return html;
}

function renderFunds() {
  const keys = Object.keys(META).filter(k => META[k].kind === 'fund');
  document.getElementById('view-funds').innerHTML = keys.map(renderFund).join('');
}
function renderHouse() {
  const keys = Object.keys(META).filter(k => META[k].kind === 'house');
  document.getElementById('view-house').innerHTML = keys.map(renderPolitician).join('');
}
function renderSenate() {
  const keys = Object.keys(META).filter(k => META[k].kind === 'senate');
  document.getElementById('view-senate').innerHTML = keys.map(renderPolitician).join('');
}
function renderConsensus() {
  const c = DATA.__consensus__ || [];
  let html = '';
  if (c.length === 0) {
    html = renderEmpty("No multi-trader overlaps found — try again after more data lands.");
  } else {
    html = '<div class="tcard" style="padding:4px 16px;">';
    for (const item of c) {
      html += '<div class="pos-row">';
      html += '<div class="pos-left"><span class="ticker">' + item.ticker + '</span>';
      html += '<span class="asset-name">' + item.longs.length + ' traders long</span></div>';
      html += '<div class="pos-right">' + item.longs.map(avatarFor).join('') + '</div></div>';
    }
    html += '</div>';
  }
  document.getElementById('view-consensus').innerHTML = html;
}
function renderRecent() {
  const feed = DATA.__recent__ || [];
  if (feed.length === 0) {
    document.getElementById('view-recent').innerHTML = renderEmpty("No recent activity yet.");
    return;
  }
  let html = '<div class="tcard" style="padding:4px 16px;">';
  for (const e of feed) {
    html += '<div class="pos-row">';
    html += '<div class="pos-left">' + avatarFor(e.who);
    html += '<div style="min-width:0;"><div style="display:flex;align-items:center;gap:8px;">';
    html += '<span class="ticker">' + (e.ticker || '???') + '</span>' + actPill(e.action) + '</div>';
    html += '<div style="font-size:11px;color:var(--text-secondary);margin-top:2px;">' + (e.asset || '') + '</div></div></div>';
    html += '<span class="pct" style="min-width:80px;">' + (e.date || '') + '</span></div>';
  }
  html += '</div>';
  document.getElementById('view-recent').innerHTML = html;
}

renderFunds(); renderHouse(); renderSenate(); renderConsensus(); renderRecent();

document.querySelectorAll('.tab').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    ['funds','house','senate','consensus','recent'].forEach(v => {
      document.getElementById('view-' + v).hidden = (v !== b.dataset.tab);
    });
  });
});
</script>
</body>
</html>
"""


def build():
    print("[1/3] Loading data from CSVs...")
    all_data = {}
    for key, meta in TRADERS.items():
        if meta["kind"] == "fund":
            all_data[key] = load_fund_holdings(key)
            print(f"      {meta['display']}: {all_data[key]['holdings_count']} positions")
        else:
            all_data[key] = load_politician_trades(key)
            print(f"      {meta['display']}: {all_data[key]['trade_count']} trades")

    print("[2/3] Computing consensus + recent feed...")
    all_data["__consensus__"] = build_consensus(all_data)
    all_data["__recent__"] = build_recent_feed(all_data)
    print(f"      Consensus picks: {len(all_data['__consensus__'])}")
    print(f"      Recent events: {len(all_data['__recent__'])}")

    print("[3/3] Writing docs/index.html...")
    html = (HTML_TEMPLATE
            .replace("__DATA_JSON__", json.dumps(all_data, default=str))
            .replace("__META_JSON__", json.dumps(TRADERS))
            .replace("__TRADER_COUNT__", str(len(TRADERS)))
            .replace("__UPDATED__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
            .replace("__REPO_URL__", "https://github.com/yourusername/trader-tracker"))

    out_path = DOCS_DIR / "index.html"
    out_path.write_text(html)
    print(f"      ✓ Wrote {out_path} ({len(html):,} bytes)")
    print(f"\nOpen with:  open {out_path}   (or push to GitHub Pages)")


if __name__ == "__main__":
    build()
