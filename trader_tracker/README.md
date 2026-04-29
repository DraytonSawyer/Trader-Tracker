# Trader Tracker — DIY Edition

A free, no-API-key pipeline that pulls congressional stock disclosures and
hedge fund 13Fs directly from the source of truth (House Clerk + SEC EDGAR)
and emails / prints whatever's new since the last run.

Three scripts:

- **`house_tracker.py`** — U.S. House members' Periodic Transaction Reports (PTRs)
- **`senate_tracker.py`** — U.S. senators' PTRs (different system, same idea)
- **`f13_tracker.py`** — Hedge fund 13F-HR quarterly holdings reports

Plus a **`build_site.py`** that reads everything in `data/` and writes a single
self-contained `docs/index.html` for GitHub Pages.

Currently configured for: Pelosi, Khanna, McCaul (House); Tuberville, Mullin,
Whitehouse (Senate); Buffett, Ackman, Klarman, Einhorn, Druckenmiller,
Aschenbrenner (13F).

## Get a real bookmarkable URL (GitHub Pages)

After pushing this folder to a GitHub repo:

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: **main** · Folder: **/docs**
4. Save. You'll get a URL like `https://yourusername.github.io/trader-tracker/`

The GitHub Actions workflow (in `.github/workflows/track.yml`) runs daily at
6am ET — pulls fresh disclosures, regenerates `docs/index.html`, and commits.
Pages picks up the change automatically. Bookmark the URL on your phone.

The site is one self-contained HTML file with all data embedded as JSON, all
CSS and JS inline. No build tools, no dependencies, works offline once cached.

## Adding more House members

Edit the `MEMBERS` dict at the top of `house_tracker.py`:

```python
MEMBERS = {
    "pelosi": {"last": "Pelosi", "first_prefix": "Nancy",   "label": "Nancy Pelosi (D-CA)"},
    "khanna": {"last": "Khanna", "first_prefix": "Ro",      "label": "Ro Khanna (D-CA)"},
    "mccaul": {"last": "McCaul", "first_prefix": "Michael", "label": "Michael McCaul (R-TX)"},
}
```

The `first_prefix` only needs to be long enough to disambiguate (e.g. several
"Smith"s in Congress would need their full first name). For most members the
last name alone is unique.

## What you get

For each run:

- `data/pelosi_trades.csv` — every transaction year-to-date, parsed into
  structured columns: ticker, owner (self/spouse/joint), buy/sell, date, dollar
  range, option strike + expiry, description, filing date.
- A printed diff in the terminal showing **only what's new** since the previous
  run.
- `data/pelosi_seen.json` — the state file used to compute the diff.

## How it works

```
┌─────────────────────────────┐
│ disclosures-clerk.house.gov │   public, no auth, daily-refreshed ZIP
└──────────────┬──────────────┘
               │  2026FD.ZIP  (~30 MB index of all 2026 filings)
               ▼
        ┌─────────────┐
        │ filter to   │   Last == "Pelosi", FilingType == "P" (PTR)
        │ Pelosi PTRs │
        └──────┬──────┘
               │  list of DocIDs
               ▼
        ┌─────────────┐
        │ download +  │   one PDF per filing (~50 KB each)
        │ parse PDFs  │   regex over text-extracted layout
        └──────┬──────┘
               ▼
   ┌────────────────────────┐
   │ CSV  +  diff vs last   │   prints only NEW transactions
   │ run (signature hashes) │
   └────────────────────────┘
```

## Setup (5 minutes)

```bash
git clone <this folder somewhere>
cd trader_tracker
pip install -r requirements.txt
python pelosi_tracker.py        # first run pulls the full year
python pelosi_tracker.py        # second run prints "no new trades"
```

## Run it on a schedule (free, forever)

The `.github/workflows/track.yml` file runs the script daily at 6am ET on
GitHub Actions and commits any changes to `data/`. Push this folder to a
private GitHub repo and the cron job activates automatically — no server
required.

To get an email/Slack ping when something new lands, add this to the workflow
after the script step:

```yaml
- name: Notify on new trades
  if: contains(steps.run.outputs.stdout, '🆕')
  uses: dawidd6/action-send-mail@v3
  with:
    to: you@example.com
    subject: "New Pelosi trade filed"
    body: ${{ steps.run.outputs.stdout }}
```

## Adding more senators

Edit the `SENATORS` dict at the top of `senate_tracker.py`:

```python
SENATORS = {
    "tuberville": {"first": "Tommy",     "last": "Tuberville", "label": "Tommy Tuberville (R-AL)"},
    "mullin":     {"first": "Markwayne", "last": "Mullin",     "label": "Markwayne Mullin (R-OK)"},
    "whitehouse": {"first": "Sheldon",   "last": "Whitehouse", "label": "Sheldon Whitehouse (D-RI)"},
}
```

The Senate scraper does three things differently from the House one:
1. POSTs the click-through agreement at `efdsearch.senate.gov/search/home/` to get a session cookie
2. Posts to a search-data endpoint with senator name + PTR filter (report_type=11)
3. Parses each filing's HTML table (Senate filings are HTML, House filings are PDF)

**Legal:** § 105(c) of the Ethics in Government Act prohibits use of Senate
disclosure data for commercial purposes. This script is for personal research.
Don't use it to power a paid service.

## Hedge funds — `f13_tracker.py`

Hedge funds file 13Fs quarterly with the SEC. The included `f13_tracker.py`
script handles **any number of funds** by CIK. Currently configured for:

- Bill Ackman / Pershing Square (CIK 0001336528)
- Michael Burry / Scion Asset Management (CIK 0001649339)
- Leopold Aschenbrenner / Situational Awareness (CIK 0002045724)

Add more by editing the `FUNDS` dict at the top of the file:

```python
FUNDS = {
    "ackman": {"cik": "0001336528", "label": "Pershing Square (Ackman)"},
    "loeb":   {"cik": "0001040273", "label": "Third Point (Loeb)"},   # add this
}
```

Find a manager's CIK by searching `https://www.sec.gov/cgi-bin/browse-edgar?company={name}&owner=include&action=getcompany`.

### How it works
1. Hits `https://data.sec.gov/submissions/CIK{cik}.json` for each fund.
2. Finds the most recent `13F-HR` filing in `filings.recent`.
3. Fetches the filing's `index.json` and locates the info table XML.
4. Parses each `<infoTable>` element (one per position).
5. Snapshots and diffs against the last run, surfacing new positions, exits,
   and >5% size changes.

### Important caveats
- **45-day filing lag.** Q4 holdings (Dec 31) aren't visible until ~Feb 14.
- **Long equity + listed options only.** Shorts are NOT disclosed.
- **No international holdings.** A US manager's overseas positions are invisible.
- **SEC requires a real `User-Agent` header** with contact info. Update the
  `HEADERS` dict in the script with your email before deploying.
- **SEC throttles aggressively** — the script paces requests at <10/sec, well
  under the limit, but be polite if you add many funds.

## Caveats — read these

- **Dollar amounts are ranges, not exact values.** The STOCK Act only
  requires brackets ($1,001–$15,000, $15,001–$50,000, etc.). The CSV captures
  both endpoints; treat the midpoint as your best estimate.
- **Filings lag execution by up to 45 days.** This is a thematic / "what is
  smart money positioning around" tool, not a copy-trading signal. By the time
  a trade is public, it's often weeks old.
- **Options trades hide the premium and exact contract count beyond the
  range.** Strike + expiry + range only.
- **Trades are filed in the Member's name** even when executed by a spouse
  (Paul Pelosi handles the family portfolio per public reporting). The `owner`
  column captures the SP/JT/DC distinction the form provides.
- **The House Clerk site rate-limits and sometimes blocks cloud IPs.** If you
  hit 403s on a VPS, run from a residential IP or proxy through one.
- **PDF parsing is regex-based.** The form layout is stable but if the Clerk
  changes the template, the parser may need tweaks. Run `test_parser.py`
  against the bundled fixture to confirm parsing still works after any change.
