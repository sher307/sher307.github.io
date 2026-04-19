#!/usr/bin/env python3
"""Build merged state dataset for the healthcare equity map.

Outputs: /workspace/data/health_map_states.json
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
import time
import urllib.request
from pathlib import Path

STATE_FIPS_TO_ABBR = {
    "01": "AL",
    "02": "AK",
    "04": "AZ",
    "05": "AR",
    "06": "CA",
    "08": "CO",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "15": "HI",
    "16": "ID",
    "17": "IL",
    "18": "IN",
    "19": "IA",
    "20": "KS",
    "21": "KY",
    "22": "LA",
    "23": "ME",
    "24": "MD",
    "25": "MA",
    "26": "MI",
    "27": "MN",
    "28": "MS",
    "29": "MO",
    "30": "MT",
    "31": "NE",
    "32": "NV",
    "33": "NH",
    "34": "NJ",
    "35": "NM",
    "36": "NY",
    "37": "NC",
    "38": "ND",
    "39": "OH",
    "40": "OK",
    "41": "OR",
    "42": "PA",
    "44": "RI",
    "45": "SC",
    "46": "SD",
    "47": "TN",
    "48": "TX",
    "49": "UT",
    "50": "VT",
    "51": "VA",
    "53": "WA",
    "54": "WV",
    "55": "WI",
    "56": "WY",
}

STATE_ABBR_TO_FIPS = {v: k for k, v in STATE_FIPS_TO_ABBR.items()}

URBAN_MEDICAL_DEBT_URL = (
    "https://raw.githubusercontent.com/UrbanInstitute/debt-interactive-map/"
    "master/state_medical_debt.csv"
)
HRSA_PRIMARY_CARE_HPSA_URL = (
    "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_PC.csv"
)
MARCH_OF_DIMES_ENDPOINT = (
    "https://www.marchofdimes.org/peristats/api/v1/data/"
    "MaternityCareDesert/MapContent?mapKey=AccessMaternityCare&fipscode={state_fips}"
    "&isExport=false"
)
PESP_FLOURISH_EMBED_URL = "https://public.flourish.studio/visualisation/16486688/embed"


def fetch_text(url: str, retries: int = 3) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json,text/plain,*/*",
        "Referer": "https://www.marchofdimes.org/peristats/",
    }
    request = urllib.request.Request(url, headers=headers)

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.8 * attempt)

    raise RuntimeError(f"Unable to fetch URL after retries: {url}")


def fetch_urban_medical_debt() -> dict[str, float]:
    raw_csv = fetch_text(URBAN_MEDICAL_DEBT_URL)
    reader = csv.DictReader(raw_csv.splitlines())
    values: dict[str, float] = {}
    for row in reader:
        state_fips = row["id"].zfill(2)
        if state_fips not in STATE_FIPS_TO_ABBR:
            continue
        debt_share = float(row["medical_debt_collect_all_st"])
        values[state_fips] = debt_share
    return values


def _parse_float(value: str) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() == "nan":
        return None
    if text.startswith("n<"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_hrsa_shortage_score() -> dict[str, float]:
    raw_csv = fetch_text(HRSA_PRIMARY_CARE_HPSA_URL)
    reader = csv.DictReader(raw_csv.splitlines())

    weighted_score_sum: dict[str, float] = {fips: 0.0 for fips in STATE_FIPS_TO_ABBR}
    weight_sum: dict[str, float] = {fips: 0.0 for fips in STATE_FIPS_TO_ABBR}

    for row in reader:
        state_fips = row.get("Primary State FIPS Code", "").zfill(2)
        if state_fips not in STATE_FIPS_TO_ABBR:
            continue
        if row.get("HPSA Status Code", "").strip() != "D":
            continue

        score = _parse_float(row.get("HPSA Score", ""))
        if score is None:
            continue

        designation_population = _parse_float(row.get("HPSA Designation Population", "")) or 0.0
        weight = max(designation_population, 1.0)

        weighted_score_sum[state_fips] += score * weight
        weight_sum[state_fips] += weight

    state_scores: dict[str, float] = {}
    for state_fips in STATE_FIPS_TO_ABBR:
        if weight_sum[state_fips] <= 0:
            state_scores[state_fips] = 0.0
        else:
            state_scores[state_fips] = weighted_score_sum[state_fips] / weight_sum[state_fips]
    return state_scores


def fetch_march_of_dimes_desert_pct() -> dict[str, float]:
    pattern = re.compile(r"\{'fips':'(?P<fips>\d{5})','value':(?P<value>\d+),'abb':null\}")
    results: dict[str, float] = {}

    for state_fips in STATE_FIPS_TO_ABBR:
        body = fetch_text(MARCH_OF_DIMES_ENDPOINT.format(state_fips=state_fips))
        county_rows = pattern.findall(body)
        if not county_rows:
            results[state_fips] = 0.0
            continue

        desert_count = 0
        total_count = 0
        for county_fips, raw_value in county_rows:
            if not county_fips.startswith(state_fips):
                continue
            value = int(raw_value)
            total_count += 1
            if value == 0:
                desert_count += 1

        if total_count == 0:
            results[state_fips] = 0.0
        else:
            results[state_fips] = desert_count / total_count

    return results


def fetch_pesp_pe_hospital_counts() -> tuple[dict[str, int], dict[str, float]]:
    html = fetch_text(PESP_FLOURISH_EMBED_URL)
    marker = "_Flourish_data = "
    start = html.find(marker)
    if start < 0:
        raise RuntimeError("Could not locate PESP Flourish data payload.")

    json_start = html.find("{", start)
    if json_start < 0:
        raise RuntimeError("Could not locate start of PESP Flourish JSON payload.")

    depth = 0
    json_end = -1
    for idx in range(json_start, len(html)):
        char = html[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                json_end = idx + 1
                break

    if json_end < 0:
        raise RuntimeError("Could not locate end of PESP Flourish JSON payload.")

    flourish_data = json.loads(html[json_start:json_end])
    regions = flourish_data.get("regions", [])

    count_by_fips: dict[str, int] = {fips: 0 for fips in STATE_FIPS_TO_ABBR}
    pct_by_fips: dict[str, float] = {fips: 0.0 for fips in STATE_FIPS_TO_ABBR}

    for item in regions:
        fips = str(item.get("id", "")).zfill(2)
        if fips not in STATE_FIPS_TO_ABBR:
            continue

        metadata = item.get("metadata", [])
        if len(metadata) >= 2:
            pct_by_fips[fips] = float(metadata[0])
            count_by_fips[fips] = int(metadata[1])

    return count_by_fips, pct_by_fips


def min_max(values: dict[str, float]) -> dict[str, float]:
    numeric_values = list(values.values())
    minimum = min(numeric_values)
    maximum = max(numeric_values)
    if math.isclose(maximum, minimum):
        return {k: 0.0 for k in values}
    return {k: (v - minimum) / (maximum - minimum) for k, v in values.items()}


def quantile_bins(values: dict[str, float], buckets: int = 3) -> dict[str, int]:
    ordered = sorted(values.values())
    quantile_values = statistics.quantiles(ordered, n=buckets, method="inclusive")
    thresholds = []
    for idx in range(1, buckets):
        # Use inclusive quantiles so every state lands in a stable 0..buckets-1 band.
        thresholds.append(quantile_values[idx - 1])

    output: dict[str, int] = {}
    for key, value in values.items():
        band = 0
        for threshold in thresholds:
            if value > threshold:
                band += 1
        output[key] = band
    return output


def build_dataset() -> list[dict]:
    urban_debt = fetch_urban_medical_debt()
    hrsa_score = fetch_hrsa_shortage_score()
    march_desert_pct = fetch_march_of_dimes_desert_pct()
    pesp_counts, pesp_pct = fetch_pesp_pe_hospital_counts()

    hrsa_norm = min_max(hrsa_score)
    march_norm = min_max(march_desert_pct)

    care_desert_index = {
        state: (hrsa_norm[state] + march_norm[state]) / 2.0 for state in STATE_FIPS_TO_ABBR
    }

    debt_bin = quantile_bins(urban_debt, buckets=3)
    desert_bin = quantile_bins(care_desert_index, buckets=3)

    rows: list[dict] = []
    for fips, abbr in STATE_FIPS_TO_ABBR.items():
        rows.append(
            {
                "state_fips": fips,
                "state_abbr": abbr,
                "medical_debt_share": round(urban_debt.get(fips, 0.0), 6),
                "medical_debt_bin": debt_bin.get(fips, 0),
                "maternity_desert_county_share": round(march_desert_pct.get(fips, 0.0), 6),
                "hrsa_primary_care_hpsa_score": round(hrsa_score.get(fips, 0.0), 4),
                "healthcare_desert_index": round(care_desert_index.get(fips, 0.0), 6),
                "healthcare_desert_bin": desert_bin.get(fips, 0),
                "pe_owned_hospitals": pesp_counts.get(fips, 0),
                "pe_owned_hospital_pct_of_private": round(pesp_pct.get(fips, 0.0), 2),
            }
        )

    rows.sort(key=lambda item: item["state_fips"])
    return rows


def main() -> None:
    from datetime import UTC, datetime

    output_path = Path("/workspace/data/health_map_states.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "notes": [
            "medical_debt_share from Urban Institute Debt in America state_medical_debt.csv",
            "maternity_desert_county_share derived from March of Dimes MaternityCareDesert map API",
            "hrsa_primary_care_hpsa_score is population-weighted avg HPSA score from HRSA BCD_HPSA_FCT_DET_PC.csv",
            "healthcare_desert_index is min-max normalized average of maternity_desert_county_share and hrsa_primary_care_hpsa_score",
            "pe_owned_hospitals and pe_owned_hospital_pct_of_private from PESP Flourish dataset (visualisation/16486688)",
        ],
        "states": build_dataset(),
    }

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
