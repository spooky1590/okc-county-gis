import os
import random
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

# --- Config ---
DATA_DIR        = os.environ.get("DATA_DIR", ".")
DB_PATH         = os.path.join(DATA_DIR, "parcels.db")
PROGRESS        = os.path.join(DATA_DIR, "progress.txt")
GDRIVE_FILE_ID  = os.environ.get("GDRIVE_FILE_ID", "")  # set in Railway vars
BASE_URL     = "https://docs.oklahomacounty.org/treasurer/AccountNumberResults.asp"
MIN_DELAY    = 1.5
MAX_DELAY    = 3.5
BURST_EVERY  = 100
BURST_SLEEP  = 12
MAX_RETRIES  = 4
TIMEOUT      = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://docs.oklahomacounty.org/treasurer/",
}


def download_db_if_missing():
    if os.path.exists(DB_PATH):
        return
    if not GDRIVE_FILE_ID:
        raise RuntimeError("parcels.db not found and GDRIVE_FILE_ID is not set")
    print("parcels.db not found — downloading from Google Drive ...")
    # confirm=t bypasses the virus-scan warning page for large files
    url = (
        f"https://drive.usercontent.google.com/download"
        f"?id={GDRIVE_FILE_ID}&export=download&authuser=0&confirm=t"
    )
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(DB_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    print(f"  Downloaded parcels.db ({os.path.getsize(DB_PATH) / 1e6:.1f} MB)")


def setup_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delinquent_taxes (
            accountno  TEXT NOT NULL,
            tax_year   TEXT NOT NULL,
            total_due  REAL NOT NULL,
            PRIMARY KEY (accountno, tax_year)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dt_accountno ON delinquent_taxes (accountno)"
    )
    conn.commit()


def load_apns(conn):
    rows = conn.execute(
        "SELECT accountno, PARCELNB FROM parcels WHERE accountno != '' AND accountno IS NOT NULL"
    ).fetchall()
    return [(apn.strip(), (alt or "").strip()) for apn, alt in rows if apn and apn.strip()]


def load_progress(progress_path):
    if os.path.exists(progress_path):
        try:
            with open(progress_path) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            pass
    return 0


def save_progress(progress_path, index):
    with open(progress_path, "w") as f:
        f.write(str(index))


def fetch_page(session, property_id):
    url = f"{BASE_URL}?PropertyID={property_id}"
    backoff = 30
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                print(f"  [429] Rate limited. Sleeping {retry_after}s ...")
                time.sleep(retry_after)
                backoff = min(backoff * 2, 300)
                continue
            if resp.status_code in (500, 502, 503, 504):
                print(f"  [HTTP {resp.status_code}] Server error. Sleeping {backoff}s ...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            print(f"  [HTTP {resp.status_code}] Skipping {property_id}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"  [Error] {e}. Sleeping {backoff}s ...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
    print(f"  [Failed] Gave up on PropertyID={property_id} after {MAX_RETRIES} attempts")
    return None


def _find_tax_table(soup):
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if not first_row:
            continue
        cells = first_row.find_all("th") or first_row.find_all("td")
        if not cells:
            continue
        if len(cells) > 10:
            continue
        headers = [c.get_text(strip=True) for c in cells]
        norm = [h.lower() for h in headers]
        if "total due" not in norm:
            continue
        col_due = norm.index("total due")
        col_year = None
        for candidate in ("tax year/id", "tax year", "year"):
            if candidate in norm:
                col_year = norm.index(candidate)
                break
        if col_year is None:
            col_year = 1 if col_due != 1 else 0
        return table, col_due, col_year
    return None


def parse_tax_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    result = _find_tax_table(soup)
    if result is None:
        return []
    table, col_due, col_year = result
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        if len(cells) <= max(col_due, col_year):
            continue
        raw_due = cells[col_due].get_text(strip=True).replace(",", "").replace("$", "")
        try:
            total_due = float(raw_due) if raw_due else 0.0
        except ValueError:
            total_due = 0.0
        tax_year = cells[col_year].get_text(strip=True)
        rows.append({"tax_year": tax_year, "total_due": total_due})
    return rows


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    download_db_if_missing()

    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)

    print("Loading APNs ...")
    apns = load_apns(conn)
    total = len(apns)
    print(f"  {total} APNs loaded.")

    start_index = load_progress(PROGRESS)
    if start_index > 0:
        resume_apn = apns[start_index][0] if start_index < total else "end"
        print(f"Resuming from index {start_index} (APN: {resume_apn})")

    session = requests.Session()
    request_count = 0

    try:
        for i in range(start_index, total):
            apn, _ = apns[i]
            property_id = apn.lstrip("Rr")

            html = fetch_page(session, property_id)

            if html is not None:
                rows = parse_tax_rows(html)
                owed = [r for r in rows if r["total_due"] > 0]

                if len(owed) >= 2:
                    conn.executemany(
                        """INSERT OR REPLACE INTO delinquent_taxes
                               (accountno, tax_year, total_due)
                           VALUES (?, ?, ?)""",
                        [(apn, r["tax_year"], r["total_due"]) for r in owed],
                    )
                    conn.commit()
                    print(f"[{i+1}/{total}] {apn}  →  {len(owed)} years owed  (SAVED)")
                else:
                    print(f"[{i+1}/{total}] {apn}  →  {len(owed)} year(s) owed  (skip)")
            else:
                print(f"[{i+1}/{total}] {apn}  →  fetch failed  (skip)")

            save_progress(PROGRESS, i + 1)
            request_count += 1

            if request_count % BURST_EVERY == 0:
                print(f"  [Burst pause] {BURST_SLEEP}s ...")
                time.sleep(BURST_SLEEP)

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    except KeyboardInterrupt:
        print("\nInterrupted by user. Progress saved — re-run to resume.")
    finally:
        conn.close()

    print(f"\nDone. Results written to {DB_PATH} → delinquent_taxes table")


if __name__ == "__main__":
    main()
