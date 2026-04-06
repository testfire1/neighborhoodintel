#!/usr/bin/env python3
"""
neighborhood_crime_report.py
════════════════════════════════════════════════════════════════════
Pull open-source crime & neighborhood stats for any US ZIP code
and generate a self-contained interactive HTML report.

Data Sources (all free / open):
  ① Nominatim / OpenStreetMap  — ZIP → lat/lon, city, state  (no key)
  ② FBI Crime Data Explorer    — annual city-level crime stats (free key)
       → Get your free key at: https://api.data.gov/signup/
  ③ Socrata Open Data portals  — recent incident-level data   (no key)
       Chicago · NYC · San Francisco · Los Angeles · Seattle · Austin · Denver

Usage:
  python neighborhood_crime_report.py 60614
  python neighborhood_crime_report.py 10001 --api-key YOUR_FBI_KEY
  python neighborhood_crime_report.py 94107 --api-key YOUR_KEY --open
  python neighborhood_crime_report.py 60614 --demo          # sample data, no key needed

Environment variable shortcut:
  export FBI_API_KEY="your_key_here"
  python neighborhood_crime_report.py 60614
"""

import sys
import json
import time
import argparse
import os
import webbrowser
import math
from datetime import datetime, timedelta

try:
    import requests
    def _http_get(url, headers=None, params=None, timeout=15):
        r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request, urllib.parse
    def _http_get(url, headers=None, params=None, timeout=15):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

# ─── Constants ────────────────────────────────────────────────────────────────

FBI_BASE    = "https://api.usa.gov/crime/fbi/cde"
NOMINATIM   = "https://nominatim.openstreetmap.org"
UA          = "NeighborhoodCrimeReporter/1.0 (open-source; github.com)"

# Offense display config
OFFENSES = [
    ("violent-crime",      "Violent Crime",         "#dc2626"),
    ("property-crime",     "Property Crime",        "#2563eb"),
    ("homicide",           "Homicide",              "#7f1d1d"),
    ("rape",               "Rape",                  "#b91c1c"),
    ("robbery",            "Robbery",               "#ea580c"),
    ("aggravated-assault", "Aggravated Assault",    "#f97316"),
    ("burglary",           "Burglary",              "#1d4ed8"),
    ("larceny",            "Larceny",               "#3b82f6"),
    ("motor-vehicle-theft","Motor Vehicle Theft",   "#60a5fa"),
    ("arson",              "Arson",                 "#d97706"),
]
OFFENSE_LABEL = {k: v for k, v, _ in OFFENSES}
OFFENSE_COLOR = {k: c for k, _, c in OFFENSES}

# Cities with open Socrata crime portals (no key needed)
SOCRATA_CITIES = {
    # state_abbr → { city_key: { dataset_id, host, zip_col, date_col, type_col } }
    "IL": {
        "chicago": {
            "host": "data.cityofchicago.org",
            "dataset": "ijzp-q8t2",
            "zip_col":  "zip_code",
            "date_col": "date",
            "type_col": "primary_type",
            "lat_col":  "latitude",
            "lon_col":  "longitude",
        }
    },
    "NY": {
        "new york city": {
            "host": "data.cityofnewyork.us",
            "dataset": "uip8-fykc",
            "zip_col":  "zip_code",
            "date_col": "cmplnt_fr_dt",
            "type_col": "ofns_desc",
            "lat_col":  "latitude",
            "lon_col":  "longitude",
        }
    },
    "CA": {
        "san francisco": {
            "host": "data.sfgov.org",
            "dataset": "wg3w-h783",
            "zip_col":  None,          # SF doesn't expose zip; we filter by lat/lon bbox
            "date_col": "incident_date",
            "type_col": "incident_category",
            "lat_col":  "latitude",
            "lon_col":  "longitude",
        },
        "los angeles": {
            "host": "data.lacity.org",
            "dataset": "2nrs-mtv8",
            "zip_col":  None,
            "date_col": "date_occ",
            "type_col": "crm_cd_desc",
            "lat_col":  "lat",
            "lon_col":  "lon",
        },
    },
    "WA": {
        "seattle": {
            "host": "data.seattle.gov",
            "dataset": "tazs-3rd5",
            "zip_col":  None,
            "date_col": "offense_start_datetime",
            "type_col": "offense",
            "lat_col":  "latitude",
            "lon_col":  "longitude",
        }
    },
    "TX": {
        "austin": {
            "host": "data.austintexas.gov",
            "dataset": "fdj4-gpfu",
            "zip_col":  "zip_code",
            "date_col": "occurred_date_time",
            "type_col": "highest_offense_desc",
            "lat_col":  "latitude",
            "lon_col":  "longitude",
        }
    },
    "CO": {
        "denver": {
            "host": "www.denvergov.org",
            "dataset": "bmtr-ccyn",
            "zip_col":  None,
            "date_col": "first_occurrence_date",
            "type_col": "offense_type_id",
            "lat_col":  "geo_lat",
            "lon_col":  "geo_lon",
        }
    },
}


# ─── Geocoding ────────────────────────────────────────────────────────────────

def geocode_zip(zip_code):
    """Resolve ZIP → lat/lon, city, state via Nominatim (no API key needed)."""
    print(f"  [1/3] Geocoding ZIP {zip_code} via OpenStreetMap...")
    url = f"{NOMINATIM}/search"
    params = {
        "postalcode": zip_code,
        "country": "US",
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    try:
        data = _http_get(url, headers={"User-Agent": UA}, params=params)
    except Exception as e:
        print(f"  ✗ Geocoding failed: {e}")
        return None

    if not data:
        print(f"  ✗ ZIP {zip_code} not found in OpenStreetMap.")
        return None

    r   = data[0]
    adr = r.get("address", {})
    city = (
        adr.get("city") or adr.get("town") or adr.get("village")
        or adr.get("suburb") or adr.get("county", "Unknown")
    )
    state_abbr = (
        adr.get("state_code") or adr.get("ISO3166-2-lvl4", "")
    ).replace("US-", "").upper()

    geo = {
        "lat":        float(r["lat"]),
        "lon":        float(r["lon"]),
        "city":       city,
        "state":      adr.get("state", ""),
        "state_abbr": state_abbr,
        "county":     adr.get("county", ""),
        "zip":        zip_code,
    }
    print(f"  ✓ Located: {city}, {state_abbr}  ({geo['lat']:.4f}, {geo['lon']:.4f})")
    return geo


# ─── FBI Crime Data ───────────────────────────────────────────────────────────

def fetch_fbi_data(state_abbr, city, api_key, start=2019, end=2023):
    """Fetch summarized offense data from FBI Crime Data Explorer API."""
    print(f"  [2/3] Fetching FBI crime data for {city}, {state_abbr}...")
    city_enc = city.replace(" ", "%20")
    url = (
        f"{FBI_BASE}/summarized/state/{state_abbr}/city/{city_enc}"
        f"/offenses/{start}/{end}"
    )
    try:
        data = _http_get(url, params={"api_key": api_key})
        rows = data.get("data", [])
    except Exception as e:
        print(f"  ✗ FBI API error: {e}")
        return None

    if not rows:
        print(f"  WARNING: No FBI data returned for '{city}, {state_abbr}'.")
        print(f"     Try --city 'Exact Name' if the city name differs from FBI records.")
        return {}

    # Pivot → { "2021": { "violent-crime": 423, ... }, ... }
    by_year = {}
    for entry in rows:
        yr      = str(entry.get("data_year", ""))
        offense = entry.get("offense", "unknown")
        count   = int(entry.get("actual", 0) or 0)
        by_year.setdefault(yr, {})[offense] = count

    years = sorted(by_year.keys())
    print(f"  ✓ FBI data for years: {', '.join(years)}")
    return by_year


# ─── Socrata Open Data ────────────────────────────────────────────────────────

def _bbox(lat, lon, miles=1.5):
    """Return a rough bounding box around a point."""
    d_lat = miles / 69.0
    d_lon = miles / (69.0 * math.cos(math.radians(lat)))
    return lat - d_lat, lon - d_lat, lat + d_lat, lon + d_lon


def fetch_socrata_incidents(geo, zip_code, days=180):
    """Try to fetch recent incident data from a Socrata city portal."""
    state  = geo.get("state_abbr", "")
    city_g = geo.get("city", "").lower()

    cfg = None
    for city_key, c in SOCRATA_CITIES.get(state, {}).items():
        if city_key in city_g or city_g in city_key:
            cfg = c
            matched_city = city_key
            break

    if cfg is None:
        return None, None

    print(f"  [3/3] Fetching recent incidents from {matched_city.title()} open data portal...")
    since  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    host   = cfg["host"]
    ds     = cfg["dataset"]
    url    = f"https://{host}/resource/{ds}.json"

    # Build query
    if cfg.get("zip_col"):
        where = f"{cfg['zip_col']}='{zip_code}' AND {cfg['date_col']} >= '{since}'"
    else:
        lat, lon = geo["lat"], geo["lon"]
        s_lat, _, n_lat, e_lon = _bbox(lat, lon, miles=1.5)
        w_lon = lon - (e_lon - lon)
        where = (
            f"{cfg['date_col']} >= '{since}' AND "
            f"{cfg['lat_col']} >= '{s_lat:.5f}' AND {cfg['lat_col']} <= '{n_lat:.5f}' AND "
            f"{cfg['lon_col']} >= '{w_lon:.5f}' AND {cfg['lon_col']} <= '{e_lon:.5f}'"
        )

    params = {"$where": where, "$limit": 2000, "$order": f"{cfg['date_col']} DESC"}
    try:
        rows = _http_get(url, params=params, timeout=20)
    except Exception as e:
        print(f"  ⚠  Socrata fetch failed: {e}")
        return None, cfg

    print(f"  ✓ Got {len(rows)} incidents from {matched_city.title()} portal (last {days} days)")
    return rows, cfg


def summarize_socrata(rows, cfg):
    """Count by crime type from Socrata rows."""
    if not rows:
        return {}
    counts = {}
    for r in rows:
        t = r.get(cfg["type_col"], "UNKNOWN")
        if t:
            counts[t.strip().title()] = counts.get(t.strip().title(), 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def extract_pins(rows, cfg, max_pins=300):
    """Extract lat/lon pins for the map."""
    pins = []
    for r in rows[:max_pins]:
        try:
            lat = float(r.get(cfg["lat_col"], "") or 0)
            lon = float(r.get(cfg["lon_col"], "") or 0)
            if lat and lon:
                pins.append({
                    "lat": lat,
                    "lon": lon,
                    "type": r.get(cfg["type_col"], "Unknown").strip().title(),
                    "date": str(r.get(cfg.get("date_col", ""), ""))[:10],
                })
        except (ValueError, TypeError):
            pass
    return pins


# ─── Demo / Sample Data ───────────────────────────────────────────────────────

DEMO_BY_YEAR = {
    "2018": {"violent-crime": 612, "property-crime": 3140, "homicide": 8, "rape": 44, "robbery": 158, "aggravated-assault": 402, "burglary": 740, "larceny": 2100, "motor-vehicle-theft": 300, "arson": 12},
    "2019": {"violent-crime": 588, "property-crime": 2980, "homicide": 7, "rape": 40, "robbery": 145, "aggravated-assault": 396, "burglary": 690, "larceny": 1990, "motor-vehicle-theft": 300, "arson": 10},
    "2020": {"violent-crime": 621, "property-crime": 2650, "homicide": 11, "rape": 38, "robbery": 162, "aggravated-assault": 410, "burglary": 620, "larceny": 1730, "motor-vehicle-theft": 300, "arson": 9},
    "2021": {"violent-crime": 670, "property-crime": 2890, "homicide": 9, "rape": 47, "robbery": 178, "aggravated-assault": 436, "burglary": 660, "larceny": 1900, "motor-vehicle-theft": 330, "arson": 11},
    "2022": {"violent-crime": 645, "property-crime": 2760, "homicide": 8, "rape": 43, "robbery": 161, "aggravated-assault": 433, "burglary": 620, "larceny": 1820, "motor-vehicle-theft": 320, "arson": 10},
}

DEMO_GEO = {
    "lat": 41.9250, "lon": -87.6481,
    "city": "Chicago", "state": "Illinois",
    "state_abbr": "IL", "county": "Cook County", "zip": "60614",
}

# Per-ZIP geo overrides used in demo mode so each report shows the correct city/location
DEMO_GEO_BY_ZIP = {
    "60614": {"lat": 41.9250, "lon": -87.6481, "city": "Chicago",       "state": "Illinois",   "state_abbr": "IL", "county": "Cook County",     "neighborhood": "Lincoln Park"},
    "60622": {"lat": 41.9082, "lon": -87.6774, "city": "Chicago",       "state": "Illinois",   "state_abbr": "IL", "county": "Cook County",     "neighborhood": "Wicker Park"},
    "60647": {"lat": 41.9217, "lon": -87.7008, "city": "Chicago",       "state": "Illinois",   "state_abbr": "IL", "county": "Cook County",     "neighborhood": "Logan Square"},
    "94107": {"lat": 37.7785, "lon": -122.3893,"city": "San Francisco", "state": "California", "state_abbr": "CA", "county": "San Francisco",   "neighborhood": "SoMa"},
    "11215": {"lat": 40.6688, "lon": -73.9826, "city": "Brooklyn",      "state": "New York",   "state_abbr": "NY", "county": "Kings County",    "neighborhood": "Park Slope"},
    "10001": {"lat": 40.7484, "lon": -73.9967, "city": "New York",      "state": "New York",   "state_abbr": "NY", "county": "New York County", "neighborhood": "Chelsea"},
    "90210": {"lat": 34.0901, "lon": -118.4065,"city": "Beverly Hills", "state": "California", "state_abbr": "CA", "county": "Los Angeles",     "neighborhood": "Beverly Hills"},
    "78701": {"lat": 30.2672, "lon": -97.7431, "city": "Austin",        "state": "Texas",      "state_abbr": "TX", "county": "Travis County",   "neighborhood": "Downtown Austin"},
    "98101": {"lat": 47.6062, "lon": -122.3321,"city": "Seattle",       "state": "Washington", "state_abbr": "WA", "county": "King County",     "neighborhood": "Downtown Seattle"},
    "80202": {"lat": 39.7545, "lon": -104.9984,"city": "Denver",        "state": "Colorado",   "state_abbr": "CO", "county": "Denver County",   "neighborhood": "Downtown Denver"},
}

DEMO_INCIDENTS = {
    "Theft": 412, "Battery": 198, "Criminal Damage": 187, "Assault": 144,
    "Deceptive Practice": 112, "Burglary": 98, "Narcotics": 87,
    "Motor Vehicle Theft": 76, "Robbery": 62, "Other Offense": 55,
}


# ─── Safety Score ─────────────────────────────────────────────────────────────

def compute_safety(by_year):
    if not by_year:
        return None, None
    yr   = max(by_year.keys())
    data = by_year[yr]
    vc   = data.get("violent-crime", 0)
    pc   = data.get("property-crime", 0)
    # Weighted heuristic — tuned against national averages
    raw  = max(0, 100 - (vc / 8) - (pc / 40))
    return int(min(100, raw)), yr


def score_band(s):
    if   s >= 75: return "Lower Risk",   "#16a34a", "#dcfce7"
    elif s >= 55: return "Moderate Risk", "#d97706", "#fef3c7"
    elif s >= 35: return "Elevated Risk", "#ea580c", "#ffedd5"
    else:         return "Higher Risk",  "#dc2626", "#fee2e2"


# ─── HTML Report ──────────────────────────────────────────────────────────────

def _js_bar(bar_labels, bar_values, bar_colors, latest_year):
    return (
        "new Chart(document.getElementById('barChart'), {"
        "  type: 'bar',"
        "  data: {"
        "    labels: " + bar_labels + ","
        "    datasets: [{"
        "      label: '" + latest_year + " Incidents',"
        "      data: " + bar_values + ","
        "      backgroundColor: " + bar_colors + ","
        "      borderRadius: 5"
        "    }]"
        "  },"
        "  options: {"
        "    responsive: true, maintainAspectRatio: true,"
        "    plugins: { legend: { display: false } },"
        "    scales: {"
        "      y: { beginAtZero: true, grid: { color: '#f3f4f6' } },"
        "      x: { ticks: { maxRotation: 40, font: { size: 10 } }, grid: { display: false } }"
        "    }"
        "  }"
        "});"
    )


def _js_trend(trend_labels, violent_trend, property_trend):
    return (
        "new Chart(document.getElementById('trendChart'), {"
        "  type: 'line',"
        "  data: {"
        "    labels: " + trend_labels + ","
        "    datasets: ["
        "      { label: 'Violent Crime', data: " + violent_trend + ", borderColor: '#dc2626', backgroundColor: '#dc262622', fill: true, tension: 0.3, pointRadius: 5 },"
        "      { label: 'Property Crime', data: " + property_trend + ", borderColor: '#2563eb', backgroundColor: '#2563eb22', fill: true, tension: 0.3, pointRadius: 5 }"
        "    ]"
        "  },"
        "  options: {"
        "    responsive: true, maintainAspectRatio: true,"
        "    plugins: { legend: { position: 'top' } },"
        "    scales: {"
        "      y: { beginAtZero: true, grid: { color: '#f3f4f6' } },"
        "      x: { grid: { display: false } }"
        "    }"
        "  }"
        "});"
    )


def _js_donut(inc_labels, inc_values, inc_colors):
    return (
        "const donut = new Chart(document.getElementById('donutChart'), {"
        "  type: 'doughnut',"
        "  data: {"
        "    labels: " + inc_labels + ","
        "    datasets: [{ data: " + inc_values + ", backgroundColor: " + inc_colors + ", borderWidth: 2, borderColor: '#fff' }]"
        "  },"
        "  options: {"
        "    responsive: true, maintainAspectRatio: true,"
        "    plugins: { legend: { display: false } },"
        "    cutout: '62%'"
        "  }"
        "});"
        "const lb = document.getElementById('legendBox');"
        "const lbls = " + inc_labels + ";"
        "const vals = " + inc_values + ";"
        "const cols = " + inc_colors + ";"
        "const tot = vals.reduce((a,b)=>a+b, 0);"
        "lb.innerHTML = lbls.map((l,i) =>"
        "  '<div style=\"display:flex;align-items:center;gap:8px;margin-bottom:7px;\">'"
        "  + '<span style=\"display:inline-block;width:12px;height:12px;border-radius:3px;background:'+cols[i]+';flex-shrink:0;\"></span>'"
        "  + '<span style=\"flex:1;color:#374151;\">'+l+'</span>'"
        "  + '<span style=\"font-weight:700;color:#111827;\">'+vals[i].toLocaleString()+'</span>'"
        "  + '<span style=\"color:#9ca3af;font-size:.8em;\">('+( vals[i]/tot*100).toFixed(1)+'%)</span>'"
        "  + '</div>'"
        ").join('');"
    )


def build_html(zip_code, geo, by_year, incident_counts, pins, demo=False):
    city        = geo.get("city", "Unknown")
    state       = geo.get("state", "")
    state_abbr  = geo.get("state_abbr", "")
    lat         = geo["lat"]
    lon         = geo["lon"]
    generated   = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    year_now    = datetime.now().year

    has_fbi     = bool(by_year)
    has_socrata = bool(incident_counts)

    years        = sorted(by_year.keys()) if has_fbi else []
    latest_year  = years[-1] if years else "N/A"
    latest_data  = by_year.get(latest_year, {}) if has_fbi else {}

    safety_score, score_year = compute_safety(by_year)
    s_label, s_color, s_bg   = score_band(safety_score) if safety_score is not None else ("N/A", "#6b7280", "#f9fafb")

    # ── Chart data ──
    trend_labels    = json.dumps(years)
    violent_trend   = json.dumps([by_year.get(y, {}).get("violent-crime", 0) for y in years])
    property_trend  = json.dumps([by_year.get(y, {}).get("property-crime", 0) for y in years])

    offense_keys    = [k for k, _, _ in OFFENSES if k in latest_data]
    bar_labels      = json.dumps([OFFENSE_LABEL.get(k, k) for k in offense_keys])
    bar_values      = json.dumps([latest_data.get(k, 0) for k in offense_keys])
    bar_colors      = json.dumps([OFFENSE_COLOR.get(k, "#6b7280") for k in offense_keys])

    # Incident donut
    inc_top         = list(incident_counts.items())[:10] if has_socrata else []
    inc_labels      = json.dumps([x[0] for x in inc_top])
    inc_values      = json.dumps([x[1] for x in inc_top])
    inc_colors      = json.dumps([
        "#dc2626","#2563eb","#d97706","#16a34a","#9333ea",
        "#0891b2","#db2777","#65a30d","#ea580c","#6b7280"
    ][:len(inc_top)])

    pins_json       = json.dumps(pins[:300])
    pin_count       = len(pins)
    incident_total  = sum(incident_counts.values()) if has_socrata else 0
    city_clean      = city.split("(")[0].strip()

    # ── Pre-build JavaScript sections (avoid nested f-strings) ──
    js_bar   = _js_bar(bar_labels, bar_values, bar_colors, latest_year) if has_fbi and offense_keys else ""
    js_trend = _js_trend(trend_labels, violent_trend, property_trend) if has_fbi and len(years) >= 2 else ""
    js_donut = _js_donut(inc_labels, inc_values, inc_colors) if has_socrata else ""

    # ── Offense table ──
    if has_fbi and latest_data:
        prev_year  = years[-2] if len(years) >= 2 else None
        prev_data  = by_year.get(prev_year, {}) if prev_year else {}
        rows_html  = ""
        for key, label, color in OFFENSES:
            if key not in latest_data:
                continue
            count = latest_data[key]
            prev  = prev_data.get(key)
            if prev and prev > 0:
                pct   = ((count - prev) / prev) * 100
                clr   = "#dc2626" if pct > 0 else "#16a34a"
                arrow = "&#9650;" if pct > 0 else "&#9660;"
                chg   = '<span style="color:' + clr + ';font-weight:600">' + arrow + " " + f"{abs(pct):.1f}%" + "</span>"
            else:
                chg = '<span style="color:#9ca3af">&#8212;</span>'
            rows_html += (
                '<tr class="trow">'
                '<td style="padding:11px 16px;border-bottom:1px solid #f3f4f6;">'
                '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + color + ';margin-right:8px;vertical-align:middle;"></span>'
                + label + "</td>"
                '<td style="padding:11px 16px;border-bottom:1px solid #f3f4f6;text-align:right;font-weight:600;">' + f"{count:,}" + "</td>"
                '<td style="padding:11px 16px;border-bottom:1px solid #f3f4f6;text-align:right;">' + chg + "</td>"
                "</tr>"
            )
        table_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:0.88rem;">'
            '<thead><tr style="background:#f9fafb;">'
            '<th style="padding:10px 16px;text-align:left;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Offense Type</th>'
            '<th style="padding:10px 16px;text-align:right;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">' + latest_year + "</th>"
            '<th style="padding:10px 16px;text-align:right;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">vs Prior Year</th>'
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
        )
    else:
        table_html = ""

    # ── Banners ──
    demo_banner = (
        '<div style="background:#fffbeb;border:1px solid #f59e0b;border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:0.88rem;color:#78350f;">'
        "<strong>Demo Mode</strong> - Data shown is illustrative sample data. "
        'Run with <code>--api-key YOUR_KEY</code> for real stats. '
        'Free FBI key: <a href="https://api.data.gov/signup/" style="color:#b45309;">api.data.gov/signup</a>'
        "</div>"
    ) if demo else ""

    no_fbi_banner = "" if (has_fbi or demo) else (
        '<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:0.88rem;color:#7f1d1d;">'
        "<strong>No FBI data</strong> - Add <code>--api-key</code> for annual crime stats. "
        'Free signup: <a href="https://api.data.gov/signup/" style="color:#991b1b;">api.data.gov/signup</a>. '
        "If the city name mismatches, try <code>--city 'Exact Name'</code>."
        "</div>"
    )

    no_socrata_note = "" if has_socrata else (
        '<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:0.88rem;color:#075985;">'
        "<strong>Real-time incidents</strong> available for Chicago, NYC, SF, LA, Seattle, Austin, Denver. "
        "This ZIP is outside those cities, so only FBI annual totals are shown."
        "</div>"
    ) if has_fbi else ""

    if safety_score is not None:
        score_block = (
            '<div style="text-align:center;padding:8px 0 4px;">'
            '<div style="display:inline-flex;align-items:center;justify-content:center;'
            "width:110px;height:110px;border-radius:50%;"
            "background:" + s_bg + ";border:4px solid " + s_color + ";"
            'font-size:2.6rem;font-weight:800;color:' + s_color + ';margin-bottom:10px;">'
            + str(safety_score) + "</div>"
            '<div style="font-size:1rem;font-weight:700;color:' + s_color + ';">' + s_label + "</div>"
            '<div style="font-size:0.78rem;color:#6b7280;margin-top:2px;">Based on ' + score_year + " FBI data &middot; 0 = highest risk &middot; 100 = lowest</div>"
            "</div>"
        )
        score_note = (
            "<hr style='margin:16px 0;border:none;border-top:1px solid #f3f4f6;'>"
            "<p style='font-size:.8rem;color:#6b7280;line-height:1.6'>Score is a heuristic based on FBI crime counts vs. national benchmarks. Not a certified index.</p>"
        )
    else:
        score_block = '<p style="color:#6b7280;padding:30px 0;text-align:center;">Add --api-key to compute score</p>'
        score_note  = ""

    map_title_extra = (
        '<span style="font-size:.75rem;font-weight:400;color:#6b7280;">(' + str(pin_count) + " recent incident pins)</span>"
    ) if pins else ""

    bar_table_section = (
        "<div class='grid2' style='margin-bottom:20px;'>"
        "<div class='card'><div class='card-title'>Offense Breakdown - " + latest_year + " (FBI)</div>"
        "<canvas id='barChart' style='max-height:300px;'></canvas></div>"
        "<div class='card'><div class='card-title'>Offense Summary</div>" + table_html + "</div>"
        "</div>"
    ) if has_fbi and offense_keys else ""

    trend_section = (
        "<div class='card' style='margin-bottom:20px;'>"
        "<div class='card-title'>Crime Trend Over Time</div>"
        "<canvas id='trendChart' style='max-height:260px;'></canvas>"
        "</div>"
    ) if has_fbi and len(years) >= 2 else ""

    socrata_section = (
        "<div class='card' style='margin-bottom:20px;'>"
        "<div class='card-title'>Recent Incident Breakdown (Last 6 Months)</div>"
        "<div style='display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:center;'>"
        "<canvas id='donutChart' style='max-height:280px;'></canvas>"
        "<div id='legendBox' style='font-size:0.82rem;'></div>"
        "</div></div>"
    ) if has_socrata else ""

    incident_total_str = "--" if not has_socrata else f"{incident_total:,}"

    html = (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        "<title>Crime Report - ZIP " + zip_code + " - " + city + ", " + state_abbr + "</title>\n"
        "<link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'/>\n"
        "<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>\n"
        "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js'></script>\n"
        "<style>\n"
        "  *{box-sizing:border-box;margin:0;padding:0;}\n"
        "  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;color:#111827;}\n"
        "  .header{background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);color:#fff;padding:28px 36px;}\n"
        "  .header h1{font-size:1.55rem;font-weight:800;letter-spacing:-.02em;}\n"
        "  .header p{opacity:.82;font-size:.9rem;margin-top:5px;}\n"
        "  .container{max-width:1120px;margin:0 auto;padding:24px 20px 48px;}\n"
        "  .card{background:#fff;border-radius:14px;padding:22px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);}\n"
        "  .card-title{font-size:1rem;font-weight:700;color:#1f2937;margin-bottom:16px;}\n"
        "  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;}\n"
        "  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px;}\n"
        "  .stat{background:#fff;border-radius:14px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center;}\n"
        "  .stat-val{font-size:1.7rem;font-weight:800;color:#1e3a5f;line-height:1;}\n"
        "  .stat-lbl{font-size:.72rem;color:#6b7280;text-transform:uppercase;letter-spacing:.07em;margin-top:6px;}\n"
        "  #map{height:400px;border-radius:10px;border:1px solid #e5e7eb;}\n"
        "  .trow:hover td{background:#f9fafb;}\n"
        "  code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:.85em;}\n"
        "  a{color:#1d4ed8;}\n"
        "  footer{text-align:center;color:#9ca3af;font-size:.78rem;margin-top:32px;padding-bottom:32px;}\n"
        "  @media(max-width:720px){.grid2,.grid3{grid-template-columns:1fr;}}\n"
        "</style>\n</head>\n<body>\n\n"
        "<div class='header'>\n"
        "  <h1>&#128205; Neighborhood Crime Report &mdash; ZIP " + zip_code + "</h1>\n"
        "  <p>" + city + ", " + state + " &nbsp;&middot;&nbsp; " + state_abbr + " &nbsp;&middot;&nbsp; Generated " + generated + "</p>\n"
        "</div>\n\n"
        "<div class='container'>\n\n"
        + demo_banner + "\n"
        + no_fbi_banner + "\n"
        + no_socrata_note + "\n\n"
        "<div class='grid3' style='margin-bottom:20px;'>\n"
        "  <div class='stat'><div class='stat-val'>" + city_clean + "</div><div class='stat-lbl'>City / Area</div></div>\n"
        "  <div class='stat'><div class='stat-val'>" + latest_year + "</div><div class='stat-lbl'>Latest FBI Data Year</div></div>\n"
        "  <div class='stat'><div class='stat-val'>" + incident_total_str + "</div><div class='stat-lbl'>Recent Incidents (6 mo)</div></div>\n"
        "</div>\n\n"
        "<div class='grid2'>\n"
        "  <div class='card'>\n"
        "    <div class='card-title'>&#128737; Safety Score</div>\n"
        "    " + score_block + "\n"
        "    " + score_note + "\n"
        "  </div>\n"
        "  <div class='card'>\n"
        "    <div class='card-title'>&#128506; Location Map " + map_title_extra + "</div>\n"
        "    <div id='map'></div>\n"
        "  </div>\n"
        "</div>\n\n"
        + bar_table_section + "\n"
        + trend_section + "\n"
        + socrata_section + "\n"
        "<div class='card' style='font-size:.85rem;color:#374151;line-height:1.75;'>\n"
        "  <div class='card-title'>&#8505;&#65039; Data Sources &amp; Methodology</div>\n"
        "  <p><strong>FBI Crime Data Explorer</strong> &mdash; Annual totals reported by local agencies to the FBI UCR/NIBRS program. "
        "Not all agencies participate. Figures reflect crimes <em>reported</em> to police. "
        "<a href='https://cde.ucr.cjis.gov/' target='_blank'>cde.ucr.cjis.gov</a></p>\n"
        "  <p style='margin-top:10px;'><strong>Socrata Open Data Portals</strong> &mdash; Real-time incident data from city police departments. "
        "Filtered to ~1.5-mile radius around the ZIP centroid or exact ZIP match.</p>\n"
        "  <p style='margin-top:10px;'><strong>Geocoding</strong> &mdash; ZIP centroid via "
        "<a href='https://nominatim.openstreetmap.org' target='_blank'>Nominatim / OpenStreetMap</a> (no key required). "
        "Map tiles &copy; OpenStreetMap contributors, CC BY-SA.</p>\n"
        "  <p style='margin-top:10px;'><strong>Safety Score</strong> &mdash; Heuristic (0&ndash;100) from reported crime rates vs. national benchmarks. "
        "Not a certified index. Use alongside personal observation and local knowledge.</p>\n"
        "</div>\n\n"
        "</div>\n\n"
        "<footer>Open-source data &middot; FBI Crime Data Explorer &middot; OpenStreetMap &middot; City Open Data Portals &middot; " + str(year_now) + "</footer>\n\n"
        "<script>\n"
        "const map = L.map('map').setView([" + str(lat) + ", " + str(lon) + "], 14);\n"
        "L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {"
        "  attribution: '&copy; <a href=\"https://openstreetmap.org\">OpenStreetMap</a>',"
        "  maxZoom: 19"
        "}).addTo(map);\n"
        "L.marker([" + str(lat) + ", " + str(lon) + "], {"
        "  icon: L.divIcon({"
        "    className: '',"
        "    html: '<div style=\"background:#1d4ed8;width:16px;height:16px;border-radius:50%;border:3px solid white;\"></div>',"
        "    iconSize:[16,16], iconAnchor:[8,8]"
        "  })"
        "}).addTo(map).bindPopup('<strong>ZIP " + zip_code + "</strong><br>" + city + ", " + state + "');\n"
        "const crimePins = " + pins_json + ";\n"
        "const pinPalette = { robbery:'#ea580c', theft:'#2563eb', battery:'#9333ea', assault:'#f97316', burglary:'#1d4ed8', narcotics:'#0f766e', homicide:'#7f1d1d' };\n"
        "function pickColor(t){ const s=(t||'').toLowerCase(); for(const [k,v] of Object.entries(pinPalette)) if(s.includes(k)) return v; return '#dc2626'; }\n"
        "crimePins.forEach(p => {\n"
        "  L.circleMarker([p.lat, p.lon], { radius:5, fillColor:pickColor(p.type), color:'white', weight:1, opacity:.9, fillOpacity:.75 })\n"
        "   .addTo(map).bindPopup('<strong>'+p.type+'</strong><br>'+p.date);\n"
        "});\n"
        + js_bar + "\n"
        + js_trend + "\n"
        + js_donut + "\n"
        "</script>\n</body>\n</html>"
    )

    return html


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a neighborhood crime report for any US ZIP code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python neighborhood_crime_report.py 60614
  python neighborhood_crime_report.py 10001 --api-key abc123 --open
  python neighborhood_crime_report.py 94107 --api-key abc123 --city "San Francisco"
  python neighborhood_crime_report.py 60614 --demo

Free FBI API key → https://api.data.gov/signup/
Set env var     → export FBI_API_KEY=your_key
        """,
    )
    parser.add_argument("zip_code",  help="US ZIP code (e.g. 94107)")
    parser.add_argument("--api-key", default=os.environ.get("FBI_API_KEY", ""),
                        help="FBI Crime Data Explorer API key")
    parser.add_argument("--city",    default=None, help="Override detected city name")
    parser.add_argument("--state",   default=None, help="Override state abbreviation (e.g. CA)")
    parser.add_argument("--output",  default=None, help="Output HTML file path")
    parser.add_argument("--open",    action="store_true", help="Open report in browser when done")
    parser.add_argument("--demo",    action="store_true", help="Use sample data (no API key needed)")
    parser.add_argument("--days",    default=180, type=int, help="Days of incident history to fetch (default 180)")
    args = parser.parse_args()

    zip_code     = args.zip_code.strip().zfill(5)
    output_path  = args.output or f"crime_report_{zip_code}.html"

    print(f"\n{'─'*55}")
    print(f"  Neighborhood Crime Report — ZIP {zip_code}")
    print(f"{'─'*55}")

    # ── Demo mode ──
    if args.demo:
        print("  Running in DEMO mode with sample data...")
        # Use per-ZIP geo if available so each report shows the correct city/location
        if zip_code in DEMO_GEO_BY_ZIP:
            override        = DEMO_GEO_BY_ZIP[zip_code]
            geo             = DEMO_GEO.copy()
            geo.update(override)
            geo["zip"]      = zip_code
            nbhd            = override.get("neighborhood", "")
            if nbhd:
                geo["city"] = f"{override['city']} — {nbhd}"
        else:
            geo             = DEMO_GEO.copy()
            geo["zip"]      = zip_code
        by_year         = DEMO_BY_YEAR
        incident_counts = DEMO_INCIDENTS
        pins            = []
        html            = build_html(zip_code, geo, by_year, incident_counts, pins, demo=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n  ✅ Demo report: {output_path}")
        if args.open:
            webbrowser.open(f"file://{os.path.abspath(output_path)}")
        return

    # ── Step 1: Geocode ──
    geo = geocode_zip(zip_code)
    if not geo:
        sys.exit(1)
    if args.city:   geo["city"]       = args.city
    if args.state:  geo["state_abbr"] = args.state.upper()
    time.sleep(1.1)  # Nominatim rate-limit courtesy

    # ── Step 2: FBI data ──
    by_year = {}
    if args.api_key:
        by_year = fetch_fbi_data(geo["state_abbr"], geo["city"], args.api_key) or {}
    else:
        print("  [2/3] Skipping FBI data — no API key.")
        print("        Get a free key: https://api.data.gov/signup/")

    # ── Step 3: Socrata incidents ──
    rows, cfg       = fetch_socrata_incidents(geo, zip_code, days=args.days)
    incident_counts = summarize_socrata(rows, cfg) if rows and cfg else {}
    pins            = extract_pins(rows, cfg) if rows and cfg else []

    if not rows:
        print("  [3/3] No Socrata city portal matched this ZIP.")

    # ── Generate HTML ──
    print(f"\n  Building report...")
    html = build_html(zip_code, geo, by_year, incident_counts, pins)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    total = sum(incident_counts.values()) if incident_counts else 0
    print(f"""
{'─'*55}
  ✅ Report ready: {output_path}
  📍 Location:  {geo['city']}, {geo['state_abbr']}
  📊 FBI years: {', '.join(sorted(by_year.keys())) if by_year else 'none (add --api-key)'}
  🔴 Incidents: {total:,} (last {args.days} days via open city portal)
{'─'*55}
""")

    if args.open:
        webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
