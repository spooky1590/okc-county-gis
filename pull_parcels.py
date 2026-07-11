#!/usr/bin/env python3
"""
Systematic parcel downloader for ArcGIS FeatureServer layers
(e.g. the Oklahoma County Assessor Tax Parcels viewer).

Why this beats click-and-drag selection:
  - It asks the server for the AUTHORITATIVE list of every OBJECTID first,
    then fetches each exactly once. No overlaps, no gaps, no silent record cap.
  - It prints a count you can verify: features written == IDs reported.

Usage:
  1. pip install requests
  2. Set LAYER_URL below to the FeatureServer layer you copied from DevTools.
  3. (Optional) Set a spatial box or a WHERE filter to limit the area.
  4. python pull_parcels.py   ->  writes parcels.db (SQLite)
"""

import sqlite3
import sys
import time
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Base URL of the layer, ENDING IN THE LAYER INDEX (no /query).
# This is the Oklahoma County Assessor public parcels view, layer 0.
LAYER_URL = ("https://services8.arcgis.com/euhkr1dAJeQBIjV0/arcgis/rest/"
             "services/TaxParcelsPublics_view/FeatureServer/0")

OUT_DB = "parcels.db"

# --- Option A: whole layer (every parcel the service has) ---
WHERE = "1=1"
GEOMETRY = None

# --- Option B: limit to a rectangle (Web Mercator, wkid 102100/3857) ---
# This box is YOUR exact selection from the URL you pasted (~2km square).
# Widen these numbers to cover more area, or leave GEOMETRY = None for all.
# GEOMETRY = {
#     "xmin": -10856975.445470681,
#     "ymin":   4231122.932927794,
#     "xmax": -10854978.347715344,
#     "ymax":   4233127.163175114,
# }

INCLUDE_GEOMETRY = False   # True -> add a WKT/coords column for each parcel
BATCH = None               # None = auto-detect from service maxRecordCount
DELAY = 0.5                # seconds to wait between batch requests (reduces ban risk)

# ---------------------------------------------------------------------------

def get(url, params, tries=4, method="GET"):
    for attempt in range(tries):
        try:
            if method == "POST":
                r = requests.post(url, data=params, timeout=90)
            else:
                r = requests.get(url, params=params, timeout=90)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 2 ** attempt
            print(f"  retry {attempt+1} after error: {e} (waiting {wait}s)", file=sys.stderr)
            time.sleep(wait)


def spatial_params():
    """Shared geometry params so the ID query and the fetch query match."""
    if not GEOMETRY:
        return {}
    import json
    return {
        "geometry": json.dumps(GEOMETRY),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "102100",
        "spatialRel": "esriSpatialRelIntersects",
    }


def main():
    query = LAYER_URL.rstrip("/") + "/query"

    # 1. Service metadata: max records per request + field names.
    meta = get(LAYER_URL, {"f": "json"})
    max_rec = BATCH or meta.get("maxRecordCount", 1000)
    oid_field = meta.get("objectIdField") or "OBJECTID"
    print(f"Layer: {meta.get('name','?')} | maxRecordCount={max_rec} | id={oid_field}")

    # 2. Authoritative list of every OBJECTID in scope (ignores the record cap).
    idp = {"where": WHERE, "returnIdsOnly": "true", "f": "json"}
    idp.update(spatial_params())
    ids = get(query, idp).get("objectIds") or []
    total = len(ids)
    print(f"Server reports {total} parcels in scope.")
    if total == 0:
        sys.exit("Nothing matched. Check LAYER_URL / GEOMETRY / WHERE.")

    # 3. Fetch in batches by explicit ID list -> each parcel fetched once.
    fields = None
    rows = []
    for i in range(0, total, max_rec):
        chunk = ids[i:i + max_rec]
        fp = {
            "objectIds": ",".join(map(str, chunk)),
            "outFields": "*",
            "returnGeometry": "true" if INCLUDE_GEOMETRY else "false",
            "outSR": "4326",
            "f": "json",
        }
        data = get(query, fp, method="POST")
        feats = data.get("features", [])
        for f in feats:
            attrs = dict(f["attributes"])
            if INCLUDE_GEOMETRY and "geometry" in f:
                attrs["_geometry"] = f["geometry"]
            rows.append(attrs)
            if fields is None:
                fields = list(attrs.keys())
        print(f"  fetched {len(rows)}/{total}")
        if DELAY:
            time.sleep(DELAY)

    # 4. Write to SQLite, union of all keys so nothing is dropped.
    allkeys = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k); allkeys.append(k)

    conn = sqlite3.connect(OUT_DB)
    conn.execute("DROP TABLE IF EXISTS parcels")
    cols_def = ", ".join(f'"{k}" TEXT' for k in allkeys)
    conn.execute(f"CREATE TABLE parcels ({cols_def})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accountno ON parcels (accountno)")
    conn.executemany(
        f"INSERT INTO parcels VALUES ({','.join(['?'] * len(allkeys))})",
        [[str(r.get(k, "") if r.get(k) is not None else "") for k in allkeys] for r in rows],
    )
    conn.commit()
    conn.close()

    print(f"\nDone. Wrote {len(rows)} rows to {OUT_DB}")
    if len(rows) != total:
        print(f"WARNING: wrote {len(rows)} but server listed {total} — "
              f"re-run; a batch may have failed.", file=sys.stderr)
    else:
        print("Verified: rows written == IDs the server reported. Nothing missed.")


if __name__ == "__main__":
    main()