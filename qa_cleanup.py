"""
Aurora Master Table — Location QA Cleanup
Cleans and standardizes location fields using the Google Places API (New).
"""

import csv
import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY", "")

INPUT_FILE = "Master_Table.csv"
OUTPUT_CLEANED = "output_cleaned.csv"
OUTPUT_FLAGGED = "output_flagged.csv"
OUTPUT_LOG = "output_log.txt"
CHECKPOINT_FILE = ".checkpoint.json"

PLACES_BASE = "https://places.googleapis.com/v1"
DETAILS_FIELDS = "id,displayName,formattedAddress,addressComponents,location,websiteUri,googleMapsUri"
SEARCH_FIELDS = "places.id,places.displayName,places.formattedAddress,places.addressComponents,places.location"

# Costs (USD) as of 2024 pricing
COST_PLACE_DETAILS = 0.017   # per call
COST_TEXT_SEARCH   = 0.032   # per call

FUZZY_THRESHOLD = 90          # minimum ratio for auto-fill
FUZZY_GAP       = 15          # top result must beat 2nd by this margin
SLEEP_MS        = 50          # ms between API calls
MAX_RETRIES     = 3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stats counters
# ---------------------------------------------------------------------------

stats = {
    "total": 0,
    "auto_corrected": 0,
    "auto_filled": 0,
    "flagged": 0,
    "skipped_no_location": 0,
    "no_change": 0,
    "api_details": 0,
    "api_search": 0,
    "fix_city_was_state": 0,
    "fix_country_filled": 0,
    "fix_neighborhood_filled": 0,
    "fix_url_added": 0,
}

# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------

def _headers(fields: str) -> dict:
    return {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": fields,
        "Content-Type": "application/json",
    }


def _get_with_retry(url: str, fields: str, params: dict | None = None) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_headers(fields), params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 503):
                wait = 2 ** attempt
                log.warning("HTTP %s on attempt %d, retrying in %ds", resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            log.error("HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Request error (%s), retrying in %ds", exc, wait)
            time.sleep(wait)
    return None


def _post_with_retry(url: str, fields: str, body: dict) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(url, headers=_headers(fields), json=body, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 503):
                wait = 2 ** attempt
                log.warning("HTTP %s on attempt %d, retrying in %ds", resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            log.error("HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Request error (%s), retrying in %ds", exc, wait)
            time.sleep(wait)
    return None


def place_details(place_id: str) -> dict | None:
    stats["api_details"] += 1
    url = f"{PLACES_BASE}/places/{place_id}"
    time.sleep(SLEEP_MS / 1000)
    return _get_with_retry(url, DETAILS_FIELDS)


def text_search(query: str) -> list[dict]:
    stats["api_search"] += 1
    url = f"{PLACES_BASE}/places:searchText"
    body = {"textQuery": query, "maxResultCount": 5}
    time.sleep(SLEEP_MS / 1000)
    result = _post_with_retry(url, SEARCH_FIELDS, body)
    if result is None:
        return []
    return result.get("places", [])

# ---------------------------------------------------------------------------
# Address component parsing
# ---------------------------------------------------------------------------

def parse_components(components: list[dict]) -> dict:
    """Return dict with keys: locality, country, country_short, neighborhood,
    admin1, admin2, postal_town."""
    out = {
        "locality": "",
        "country": "",
        "country_short": "",
        "neighborhood": "",
        "admin1": "",
        "admin2": "",
        "postal_town": "",
    }
    for comp in components:
        types = comp.get("types", [])
        long_text  = comp.get("longText", "")
        short_text = comp.get("shortText", "")
        if "locality" in types:
            out["locality"] = long_text
        if "country" in types:
            out["country"] = long_text
            out["country_short"] = short_text
        if "neighborhood" in types:
            out["neighborhood"] = long_text
        if "sublocality_level_1" in types and not out["neighborhood"]:
            out["neighborhood"] = long_text
        if "administrative_area_level_1" in types:
            out["admin1"] = long_text
        if "administrative_area_level_2" in types:
            out["admin2"] = long_text
        if "postal_town" in types:
            out["postal_town"] = long_text
    return out


def best_city(parsed: dict) -> str:
    """Return the best city value from parsed components."""
    return parsed["locality"] or parsed["postal_town"] or parsed["admin2"] or ""

# ---------------------------------------------------------------------------
# Row-level QA logic
# ---------------------------------------------------------------------------

def apply_corrections(row: dict, details: dict) -> tuple[list[str], list[str]]:
    """
    Compare API details against row values and mutate row in-place.
    Returns (changes_list, proof_parts).
    """
    components = details.get("addressComponents", [])
    parsed = parse_components(components)
    website = details.get("websiteUri", "")
    canonical_id = details.get("id", "")

    changes = []
    proof_parts = [
        f'formattedAddress="{details.get("formattedAddress", "")}"',
        f'locality="{parsed["locality"]}"',
        f'country="{parsed["country"]}"',
        f'place_id="{canonical_id}"',
    ]

    # --- Place_ID: update to canonical ID
    if canonical_id and canonical_id != row["Master_Place_ID"].strip():
        old = row["Master_Place_ID"]
        row["Master_Place_ID"] = canonical_id
        changes.append(f'Master_Place_ID: "{old}" -> "{canonical_id}"')

    # --- City
    google_city = best_city(parsed)
    current_city = row["Master_City"].strip()
    if google_city and current_city != google_city:
        # Check if the current city matches the state (the specific bug)
        is_state_bug = (
            parsed["admin1"] and
            current_city.lower() == parsed["admin1"].lower()
        )
        if is_state_bug:
            stats["fix_city_was_state"] += 1
        changes.append(f'Master_City: "{current_city}" -> "{google_city}"')
        row["Master_City"] = google_city

    # --- Country
    google_country = parsed["country"]
    current_country = row["Master_Country"].strip()
    if google_country and current_country != google_country:
        if not current_country:
            stats["fix_country_filled"] += 1
        changes.append(f'Master_Country: "{current_country}" -> "{google_country}"')
        row["Master_Country"] = google_country

    # --- Neighborhood (fill only if empty)
    google_nbhd = parsed["neighborhood"]
    current_nbhd = row["Master_Neighborhood"].strip()
    if google_nbhd and not current_nbhd:
        stats["fix_neighborhood_filled"] += 1
        changes.append(f'Master_Neighborhood: "" -> "{google_nbhd}"')
        row["Master_Neighborhood"] = google_nbhd

    # --- URL (fill only if empty)
    current_url = row["Master_URL"].strip()
    if website and not current_url:
        stats["fix_url_added"] += 1
        changes.append(f'Master_URL: "" -> "{website}"')
        row["Master_URL"] = website

    return changes, proof_parts


def tier1_process(row: dict) -> dict:
    """Process a row with an existing valid Place_ID."""
    pid = row["Master_Place_ID"].strip()
    details = place_details(pid)

    if details is None:
        return _flag(row, "API_ERROR", "Place Details call failed or returned error")

    if "error" in details or not details.get("id"):
        return _flag(row, "PLACE_ID_NOT_FOUND", f"API returned error or empty for Place_ID {pid}")

    changes, proof_parts = apply_corrections(row, details)

    # Check for significant city disagreement that might be intentional
    components = details.get("addressComponents", [])
    parsed = parse_components(components)
    google_city = best_city(parsed)
    current_city_after = row["Master_City"].strip()
    if (google_city and current_city_after != google_city and
            _is_significant_mismatch(current_city_after, google_city, parsed)):
        row["QA_Action"] = "FLAGGED"
        row["QA_Changes"] = "; ".join(changes) if changes else ""
        row["QA_Proof"] = "; ".join(proof_parts)
        row["QA_Confidence"] = ""
        row["Flag_Reason"] = f"CITY_LOCALITY_MISMATCH_NEEDS_REVIEW: row={current_city_after!r} google={google_city!r}"
        row["Suggested_Correction"] = google_city
        stats["flagged"] += 1
        return row

    row["QA_Proof"] = "; ".join(proof_parts)
    row["QA_Confidence"] = ""
    if changes:
        row["QA_Action"] = "AUTO_CORRECTED"
        row["QA_Changes"] = "; ".join(changes)
        stats["auto_corrected"] += 1
    else:
        row["QA_Action"] = "NO_CHANGE"
        row["QA_Changes"] = ""
        stats["no_change"] += 1
    row["Flag_Reason"] = ""
    row["Suggested_Correction"] = ""
    return row


def tier2_process(row: dict) -> dict:
    """Process a row missing a Place_ID via Text Search."""
    name    = row["Name"].strip()
    city    = row["Master_City"].strip()
    country = row["Master_Country"].strip()
    address = row.get("Master address", "").strip()

    if not name:
        return _flag(row, "NO_USABLE_IDENTIFIERS", "No name available for text search")

    query_parts = [name]
    if city:
        query_parts.append(city)
    elif address:
        query_parts.append(address)
    if country:
        query_parts.append(country)
    query = " ".join(query_parts)

    results = text_search(query)

    if not results:
        return _flag(row, "NO_API_RESULTS", f"Text search returned no results for query: {query!r}")

    top = results[0]
    top_name = top.get("displayName", {}).get("text", "")
    top_score = fuzz.ratio(name.lower(), top_name.lower())

    # Check 2nd result gap
    if len(results) >= 2:
        second_name = results[1].get("displayName", {}).get("text", "")
        second_score = fuzz.ratio(name.lower(), second_name.lower())
        gap = top_score - second_score
    else:
        gap = 100  # only one result

    # Country check
    if country:
        top_components = top.get("addressComponents", [])
        top_parsed = parse_components(top_components)
        country_match = (
            not top_parsed["country"] or
            top_parsed["country"].lower() == country.lower()
        )
    else:
        country_match = True

    # Confidence gates
    if top_score < FUZZY_THRESHOLD:
        return _flag(row, f"LOW_NAME_MATCH_SCORE_{top_score}",
                     f"Best match {top_name!r} scored {top_score} (threshold {FUZZY_THRESHOLD})",
                     suggested=top.get("id", ""))

    if len(results) >= 2 and gap < FUZZY_GAP:
        return _flag(row, f"MULTIPLE_AMBIGUOUS_RESULTS_GAP_{gap}",
                     f"Top match {top_name!r} ({top_score}) only {gap}pts ahead of 2nd result",
                     suggested=top.get("id", ""))

    if not country_match:
        return _flag(row, "COUNTRY_MISMATCH",
                     f"Top result country {top_parsed['country']!r} != row country {country!r}",
                     suggested=top.get("id", ""))

    # Confident — fill Place_ID and run Tier 1
    row["Master_Place_ID"] = top.get("id", "")
    row = tier1_process(row)

    # Override action to AUTO_FILLED and record confidence
    if row["QA_Action"] in ("AUTO_CORRECTED", "NO_CHANGE"):
        row["QA_Action"] = "AUTO_FILLED"
        row["QA_Confidence"] = str(top_score)
        stats["auto_corrected"] = max(0, stats["auto_corrected"] - 1)
        stats["no_change"] = max(0, stats["no_change"] - 1)
        stats["auto_filled"] += 1

    return row


def _flag(row: dict, reason: str, detail: str, suggested: str = "") -> dict:
    row["QA_Action"] = "FLAGGED"
    row["QA_Changes"] = ""
    row["QA_Proof"] = ""
    row["QA_Confidence"] = ""
    row["Flag_Reason"] = f"{reason}: {detail}"
    row["Suggested_Correction"] = suggested
    stats["flagged"] += 1
    return row


def _is_significant_mismatch(row_city: str, google_city: str, parsed: dict) -> bool:
    """
    True if the mismatch looks intentional (e.g., metro area vs. actual city)
    and should be flagged rather than auto-corrected.
    Only fires post-correction, so row_city is already the corrected value.
    We flag when the row city is a real city name that differs from locality
    and neither is a state/country.
    """
    if not row_city or not google_city:
        return False
    admin1 = parsed.get("admin1", "").lower()
    if row_city.lower() == admin1:
        return False  # it's a state bug, already handled
    ratio = fuzz.ratio(row_city.lower(), google_city.lower())
    return ratio < 80  # only flag if substantially different


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if Path(CHECKPOINT_FILE).exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"processed_indices": [], "stats": {}}


def save_checkpoint(processed_indices: list[int]) -> None:
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"processed_indices": processed_indices, "stats": stats}, f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aurora Master Table Location QA")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only this many rows (for phased testing)")
    parser.add_argument("--tier1-only", action="store_true",
                        help="Only process Tier 1 (rows with Place_ID)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint")
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("ERROR: GOOGLE_API_KEY not found in environment / .env file")

    # --- Load CSV
    with open(INPUT_FILE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fieldnames = reader.fieldnames or []
        all_rows = list(reader)

    extra_cols = ["QA_Action", "QA_Changes", "QA_Proof", "QA_Confidence",
                  "Flag_Reason", "Suggested_Correction"]
    output_fieldnames = original_fieldnames + extra_cols

    # Initialize extra cols on every row
    for row in all_rows:
        for col in extra_cols:
            row[col] = ""

    stats["total"] = len(all_rows)

    # Identify which rows to process
    tier1_rows = [i for i, r in enumerate(all_rows) if r["Master_Place_ID"].strip().startswith("ChIJ")]
    tier2_rows = [i for i, r in enumerate(all_rows)
                  if not r["Master_Place_ID"].strip().startswith("ChIJ") and
                  (r["Master_City"].strip() or r.get("Master address", "").strip()) and
                  r["Name"].strip()]
    skipped_rows = [i for i, r in enumerate(all_rows)
                    if not r["Master_Place_ID"].strip().startswith("ChIJ") and
                    not r["Master_City"].strip() and
                    not r.get("Master address", "").strip()]

    stats["skipped_no_location"] = len(skipped_rows)
    for i in skipped_rows:
        all_rows[i]["QA_Action"] = "FLAGGED"
        all_rows[i]["Flag_Reason"] = "NO_LOCATION_DATA: No Place_ID, City, or Address available"
        stats["flagged"] += 1

    # Resume from checkpoint
    already_done: set[int] = set()
    if args.resume:
        cp = load_checkpoint()
        already_done = set(cp.get("processed_indices", []))
        if already_done:
            log.info("Resuming: %d rows already processed", len(already_done))

    # Apply --limit
    work_t1 = [i for i in tier1_rows if i not in already_done]
    work_t2 = [] if args.tier1_only else [i for i in tier2_rows if i not in already_done]

    if args.limit is not None:
        # Limit applies to Tier 1 only for phased testing
        work_t1 = work_t1[:args.limit]
        work_t2 = [] if args.tier1_only else work_t2[:max(0, args.limit - len(work_t1))]

    n_details = len(work_t1) + len(work_t2)   # Tier 2 also calls details after search
    n_search  = len(work_t2)

    est_cost = n_details * COST_PLACE_DETAILS + n_search * COST_TEXT_SEARCH
    print(f"\nAbout to make ~{n_details} Place Details calls and ~{n_search} Text Search calls.")
    print(f"Estimated cost: ${est_cost:.2f}  (Details: ${n_details*COST_PLACE_DETAILS:.2f}, Search: ${n_search*COST_TEXT_SEARCH:.2f})")
    answer = input("Continue? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    run_start = datetime.now()
    processed: list[int] = list(already_done)

    # --- Tier 1
    if work_t1:
        log.info("Starting Tier 1: %d rows with Place_IDs", len(work_t1))
        for idx, row_idx in enumerate(tqdm(work_t1, desc="Tier 1 (Place_ID)", unit="row")):
            try:
                all_rows[row_idx] = tier1_process(all_rows[row_idx])
            except Exception as exc:
                log.error("Row %d crashed: %s", row_idx, exc)
                all_rows[row_idx] = _flag(all_rows[row_idx], "PROCESSING_ERROR", str(exc))
            processed.append(row_idx)
            if (idx + 1) % 100 == 0:
                save_checkpoint(processed)
                log.info("Checkpoint saved at %d rows", len(processed))

    # --- Tier 2
    if work_t2:
        log.info("Starting Tier 2: %d rows without Place_IDs", len(work_t2))
        for idx, row_idx in enumerate(tqdm(work_t2, desc="Tier 2 (Text Search)", unit="row")):
            try:
                all_rows[row_idx] = tier2_process(all_rows[row_idx])
            except Exception as exc:
                log.error("Row %d crashed: %s", row_idx, exc)
                all_rows[row_idx] = _flag(all_rows[row_idx], "PROCESSING_ERROR", str(exc))
            processed.append(row_idx)
            if (idx + 1) % 100 == 0:
                save_checkpoint(processed)
                log.info("Checkpoint saved at %d rows", len(processed))

    run_end = datetime.now()

    # --- Write output_cleaned.csv
    with open(OUTPUT_CLEANED, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    log.info("Wrote %s", OUTPUT_CLEANED)

    # --- Write output_flagged.csv
    flagged_rows = [r for r in all_rows if r.get("QA_Action") == "FLAGGED"]
    with open(OUTPUT_FLAGGED, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flagged_rows)
    log.info("Wrote %s (%d flagged rows)", OUTPUT_FLAGGED, len(flagged_rows))

    # --- Write output_log.txt
    cost_details = stats["api_details"] * COST_PLACE_DETAILS
    cost_search  = stats["api_search"]  * COST_TEXT_SEARCH
    cost_total   = cost_details + cost_search

    log_text = f"""Aurora Master Table QA — Run Summary
====================================
Run started:  {run_start.strftime('%Y-%m-%d %H:%M:%S')}
Run completed: {run_end.strftime('%Y-%m-%d %H:%M:%S')}
Total rows processed: {stats['total']}
  - Auto-corrected (had Place_ID):      {stats['auto_corrected']}
  - Auto-filled (Place_ID found via search): {stats['auto_filled']}
  - No change needed:                   {stats['no_change']}
  - Flagged for review:                 {stats['flagged']}
  - No location data, skipped:          {stats['skipped_no_location']}

API calls made:
  - Place Details: {stats['api_details']}  (~${cost_details:.2f} estimated)
  - Text Search:   {stats['api_search']}  (~${cost_search:.2f} estimated)
  - Total estimated cost: ${cost_total:.2f}

Common issues detected:
  - City was actually a state:     {stats['fix_city_was_state']} rows
  - Missing country filled in:     {stats['fix_country_filled']} rows
  - Missing neighborhood filled in:{stats['fix_neighborhood_filled']} rows
  - URL added from Google:         {stats['fix_url_added']} rows
"""

    with open(OUTPUT_LOG, "w", encoding="utf-8") as f:
        f.write(log_text)
    log.info("Wrote %s", OUTPUT_LOG)
    print("\n" + log_text)

    # Clean up checkpoint on full successful run
    if args.limit is None and not args.tier1_only:
        if Path(CHECKPOINT_FILE).exists():
            os.remove(CHECKPOINT_FILE)
            log.info("Checkpoint file removed (full run complete)")


if __name__ == "__main__":
    main()
