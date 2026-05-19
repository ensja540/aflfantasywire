# AFLFantasyWire — Backend Data Fetcher

## What it does
Scrapes Footywire for live AFL fantasy stats every 15 minutes and writes
`players.json` next to `aflfantasywire.html`. The app loads this file
automatically on startup — no rebuild needed.

**Data sources:**
| Source | Data |
|--------|------|
| Footywire SC stats | SC prices, averages, break-evens, round scores |
| Footywire DT stats | AFL Fantasy prices, DT averages, DT break-evens |
| Footywire injury list | Injury status, ETA for each player |
| Footywire selection changes | Team selections, role changes |
| Footywire player profiles | Disposals, clearances, tackles, goals per game |

News and social commentary (Twitter/X, Herald Sun, Champion Data) is
handled separately and layered on by the app itself.

## Setup

```bash
pip install requests beautifulsoup4 lxml
cp config.example.json config.json
# Edit config.json if you want SC/AFLF ownership data (optional)
python fetch_data.py
```

## ⚠️ Must run from a home machine
Footywire blocks all cloud server IPs. The scraper **must** run from
a residential internet connection (home or office). If you get 403 errors,
you're running it on a server — bring it home.

## Schedule it

**Mac/Linux (crontab):**
```
crontab -e
# Add this line:
*/15 * * * * cd /path/to/backend && python3 fetch_data.py >> fetch.log 2>&1
```

**Windows (Task Scheduler):**
- Program: `python`  
- Arguments: `C:\path\to\backend\fetch_data.py`
- Trigger: Repeat every 15 minutes

**Or just run it continuously:**
```bash
./run.sh
```

## Output
`players.json` is written to the parent folder (next to `aflfantasywire.html`).
Drop both files in the same folder and open the HTML in your browser.
The badge in the app header will switch from "Mock" to "Live Data".
