"""
NeighborhoodIntel — Flask API
═══════════════════════════════════════════════════════════════════
Serves on-demand neighborhood crime reports for any US ZIP code.

Endpoints:
  GET  /                        → Landing page (search by ZIP)
  GET  /report/<zip>            → Full HTML crime report (cached 24h)
  GET  /api/zip/<zip>           → Raw JSON data for a ZIP
  GET  /dashboard               → Full dashboard UI
  GET  /health                  → Health check

Environment variables:
  FBI_API_KEY    → Free key from https://api.data.gov/signup/
  CACHE_TTL      → Cache lifetime in seconds (default: 86400 = 24h)
  PORT           → Port to listen on (default: 5000)

Quick start:
  pip install flask flask-cors requests
  export FBI_API_KEY=your_key
  python app.py
"""

import os
import json
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory, redirect
from flask_cors import CORS

# Import our report generation functions
from neighborhood_crime_report import (
    geocode_zip,
    fetch_fbi_data,
    fetch_socrata_incidents,
    summarize_socrata,
    extract_pins,
    build_html,
    DEMO_GEO,
    DEMO_BY_YEAR,
    DEMO_INCIDENTS,
)

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)  # Allow all origins — tighten with origins=["https://yourdomain.com"] in prod

FBI_API_KEY = os.environ.get("FBI_API_KEY", "")
CACHE_TTL   = int(os.environ.get("CACHE_TTL", 86400))   # 24 hours default
CACHE_DIR   = Path(os.environ.get("CACHE_DIR", "report_cache"))
CACHE_DIR.mkdir(exist_ok=True)

# Simple in-memory rate limiter (per IP, per minute)
_rate: dict = {}
RATE_LIMIT  = 10  # requests per minute per IP

# ── Helpers ───────────────────────────────────────────────────────────────────

def _cache_path(zip_code: str) -> Path:
    return CACHE_DIR / f"report_{zip_code}.html"


def _cache_valid(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL


def _rate_ok(ip: str) -> bool:
    now = time.time()
    window = now - 60
    hits = [t for t in _rate.get(ip, []) if t > window]
    _rate[ip] = hits
    if len(hits) >= RATE_LIMIT:
        return False
    _rate[ip].append(now)
    return True


def _zip_valid(z: str) -> bool:
    return z.isdigit() and len(z) == 5


# ── Report generation ─────────────────────────────────────────────────────────

def generate_report(zip_code: str, force: bool = False) -> tuple[str, dict]:
    """
    Generate (or load from cache) a full HTML report for a ZIP code.
    Returns (html_string, meta_dict).
    """
    cache = _cache_path(zip_code)

    if not force and _cache_valid(cache):
        log.info("Cache hit: %s", zip_code)
        return cache.read_text(encoding="utf-8"), {"source": "cache"}

    log.info("Generating report for %s ...", zip_code)

    # 1. Geocode
    geo = geocode_zip(zip_code)
    if not geo:
        return None, {"error": f"ZIP code {zip_code} not found"}

    time.sleep(0.5)  # Nominatim courtesy delay

    # 2. FBI data
    by_year = {}
    if FBI_API_KEY:
        by_year = fetch_fbi_data(geo["state_abbr"], geo["city"], FBI_API_KEY) or {}
    else:
        log.warning("No FBI_API_KEY set — report will show map and links only")

    # 3. Socrata live incidents
    rows, cfg       = fetch_socrata_incidents(geo, zip_code)
    incident_counts = summarize_socrata(rows, cfg) if rows and cfg else {}
    pins            = extract_pins(rows, cfg)       if rows and cfg else []

    # 4. Build HTML
    html = build_html(zip_code, geo, by_year, incident_counts, pins)
    cache.write_text(html, encoding="utf-8")

    meta = {
        "source":     "generated",
        "zip":        zip_code,
        "city":       geo.get("city"),
        "state":      geo.get("state_abbr"),
        "fbi_years":  sorted(by_year.keys()),
        "incidents":  sum(incident_counts.values()),
        "pins":       len(pins),
        "generated":  datetime.utcnow().isoformat() + "Z",
    }
    log.info("Report ready: %s — %s, %s", zip_code, geo["city"], geo["state_abbr"])
    return html, meta


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "fbi_key":   bool(FBI_API_KEY),
        "cache_dir": str(CACHE_DIR),
        "cached":    len(list(CACHE_DIR.glob("report_*.html"))),
        "time":      datetime.utcnow().isoformat() + "Z",
    })


@app.route("/")
def index():
    """Landing page — search form."""
    return Response(LANDING_HTML, mimetype="text/html")


@app.route("/dashboard")
def dashboard():
    """Serve the full dashboard UI."""
    p = Path("dashboard_v2.html")
    if p.exists():
        return Response(p.read_text(encoding="utf-8"), mimetype="text/html")
    return redirect("/")


@app.route("/report/<zip_code>")
def report(zip_code: str):
    """Return a full HTML crime report for a ZIP code."""
    ip    = request.headers.get("X-Forwarded-For", request.remote_addr)
    force = request.args.get("refresh") == "1"

    if not _zip_valid(zip_code):
        return Response("Invalid ZIP code. Must be 5 digits.", status=400)
    if not _rate_ok(ip):
        return Response("Rate limit exceeded. Please wait a moment.", status=429)

    html, meta = generate_report(zip_code.zfill(5), force=force)

    if html is None:
        error_msg = meta.get("error", "Could not generate report")
        return Response(_error_page(zip_code, error_msg), status=404, mimetype="text/html")

    resp = Response(html, mimetype="text/html")
    resp.headers["X-Report-Source"]    = meta.get("source", "unknown")
    resp.headers["X-Report-Generated"] = meta.get("generated", "")
    resp.headers["Cache-Control"]      = f"public, max-age={CACHE_TTL}"
    return resp


@app.route("/api/zip/<zip_code>")
def api_zip(zip_code: str):
    """Return JSON data for a ZIP (geocode + meta, no HTML)."""
    if not _zip_valid(zip_code):
        return jsonify({"error": "Invalid ZIP code"}), 400

    geo = geocode_zip(zip_code.zfill(5))
    if not geo:
        return jsonify({"error": f"ZIP {zip_code} not found"}), 404

    return jsonify({
        "zip":    zip_code,
        "city":   geo.get("city"),
        "state":  geo.get("state"),
        "state_abbr": geo.get("state_abbr"),
        "county": geo.get("county"),
        "lat":    geo.get("lat"),
        "lon":    geo.get("lon"),
        "report_url":  f"/report/{zip_code}",
        "cached": _cache_valid(_cache_path(zip_code)),
    })


@app.route("/api/cache")
def api_cache():
    """List all cached reports with age."""
    now = time.time()
    items = []
    for f in sorted(CACHE_DIR.glob("report_*.html")):
        age = int(now - f.stat().st_mtime)
        items.append({
            "zip":       f.stem.replace("report_", ""),
            "size_kb":   round(f.stat().st_size / 1024, 1),
            "age_secs":  age,
            "expires_in": max(0, CACHE_TTL - age),
        })
    return jsonify({"count": len(items), "reports": items})


@app.route("/api/cache/<zip_code>", methods=["DELETE"])
def delete_cache(zip_code: str):
    """Bust the cache for a specific ZIP."""
    p = _cache_path(zip_code)
    if p.exists():
        p.unlink()
        return jsonify({"deleted": zip_code})
    return jsonify({"error": "Not in cache"}), 404


# ── Static HTML strings ───────────────────────────────────────────────────────

def _error_page(zip_code: str, message: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Report Error</title>
<style>body{{font-family:sans-serif;background:#0f172a;color:#f1f5f9;display:flex;
align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px;text-align:center;}}
code{{background:#1e293b;padding:4px 10px;border-radius:6px;color:#60a5fa;}}
a{{color:#3b82f6;}}</style></head><body>
<div style="font-size:3rem">⚠️</div>
<h2>Could not generate report for ZIP {zip_code}</h2>
<p style="color:#94a3b8;max-width:400px">{message}</p>
<a href="/">← Try another ZIP</a>
</body></html>"""


LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeighborhoodIntel — Crime & Neighborhood Reports</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f172a;color:#f1f5f9;min-height:100vh;
       display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;}
  .card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:40px 48px;
        max-width:520px;width:100%;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.4);}
  h1{font-size:1.6rem;font-weight:800;letter-spacing:-.03em;margin-bottom:6px;}
  h1 span{color:#3b82f6;}
  .sub{font-size:.9rem;color:#64748b;margin-bottom:32px;line-height:1.6;}
  .search-row{display:flex;gap:8px;margin-bottom:14px;}
  input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:8px;
        color:#f1f5f9;padding:12px 16px;font-size:1rem;outline:none;transition:border .15s;}
  input:focus{border-color:#3b82f6;}
  input::placeholder{color:#475569;}
  button{background:#2563eb;color:white;border:none;border-radius:8px;
         padding:12px 22px;font-size:.95rem;font-weight:700;cursor:pointer;transition:background .15s;}
  button:hover{background:#1d4ed8;}
  .examples{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:28px;}
  .ex{background:#0f172a;border:1px solid #334155;border-radius:6px;padding:4px 12px;
      font-size:.8rem;color:#94a3b8;cursor:pointer;transition:all .15s;}
  .ex:hover{border-color:#3b82f6;color:#60a5fa;}
  .divider{border:none;border-top:1px solid #334155;margin:24px 0;}
  .features{display:grid;grid-template-columns:1fr 1fr;gap:12px;text-align:left;}
  .feat{background:#0f172a;border-radius:8px;padding:12px 14px;font-size:.8rem;color:#94a3b8;line-height:1.5;}
  .feat strong{display:block;color:#f1f5f9;margin-bottom:2px;font-size:.82rem;}
  .dashboard-link{margin-top:20px;font-size:.82rem;color:#64748b;}
  .dashboard-link a{color:#3b82f6;text-decoration:none;}
  .api-note{margin-top:12px;font-size:.75rem;color:#475569;}
  code{background:#0f172a;padding:2px 6px;border-radius:4px;color:#60a5fa;font-size:.8rem;}
</style>
</head>
<body>
<div class="card">
  <h1>Neighborhood<span>Intel</span></h1>
  <p class="sub">Free neighborhood crime &amp; safety reports for any US ZIP code.<br>
  Powered by FBI Crime Data, city open data portals, and OpenStreetMap.</p>

  <form action="/report/" method="get" onsubmit="go(event)">
    <div class="search-row">
      <input id="zipInput" type="text" placeholder="Enter ZIP code (e.g. 94107)"
             maxlength="5" inputmode="numeric" autocomplete="off" />
      <button type="submit">Search &#8594;</button>
    </div>
  </form>

  <div class="examples">
    <span class="ex" onclick="load('60614')">60614 · Lincoln Park</span>
    <span class="ex" onclick="load('94107')">94107 · SoMa SF</span>
    <span class="ex" onclick="load('10001')">10001 · Chelsea NYC</span>
    <span class="ex" onclick="load('90210')">90210 · Beverly Hills</span>
    <span class="ex" onclick="load('78701')">78701 · Austin TX</span>
  </div>

  <hr class="divider">

  <div class="features">
    <div class="feat"><strong>📊 FBI Crime Data</strong>Annual totals by offense type with year-over-year trends</div>
    <div class="feat"><strong>🔴 Live Incidents</strong>Real-time police reports for 7 major metro areas</div>
    <div class="feat"><strong>🗺 Interactive Maps</strong>Location map with incident pins and topo view</div>
    <div class="feat"><strong>🛡 Safety Score</strong>Heuristic score based on FBI violent &amp; property crime</div>
    <div class="feat"><strong>🔗 Research Links</strong>18 deep links to Niche, AreaVibes, City-Data &amp; more</div>
    <div class="feat"><strong>📎 Embeddable</strong>Every report is a self-contained iframe-ready HTML file</div>
  </div>

  <div class="dashboard-link">
    Want the full dashboard? <a href="/dashboard">Open NeighborhoodIntel Dashboard →</a>
  </div>
  <div class="api-note">
    API: <code>GET /report/&lt;zip&gt;</code> &nbsp;|&nbsp; <code>GET /api/zip/&lt;zip&gt;</code>
    &nbsp;|&nbsp; <code>GET /health</code>
  </div>
</div>

<script>
  function load(zip) {
    document.getElementById('zipInput').value = zip;
    window.location.href = '/report/' + zip;
  }
  function go(e) {
    e.preventDefault();
    const zip = document.getElementById('zipInput').value.trim();
    if (zip.length === 5 && /^\\d+$/.test(zip)) window.location.href = '/report/' + zip;
  }
</script>
</body>
</html>"""


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    log.info("Starting NeighborhoodIntel API on port %d", port)
    log.info("FBI API key: %s", "set" if FBI_API_KEY else "NOT SET (set FBI_API_KEY env var)")
    app.run(host="0.0.0.0", port=port, debug=debug)
