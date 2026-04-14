#!/usr/bin/env python3
"""
generate_terrain_zones.py
─────────────────────────────────────────────────────────────────────────────
Queries USGS 3DEP elevation along transects of the Cheboygan River centerline,
computes terrain-aware flood zone boundary polygons for partial and full breach
scenarios, and writes the pre-computed coordinates into cheboygan_flood_map_v3.html.

Run once after updating the RCL coordinates. Takes ~30–60 seconds due to API calls.

Requirements:  Python 3.8+  (no third-party packages needed)
Usage:         python generate_terrain_zones.py
Output:        cheboygan_flood_map_v3.html is updated in-place.
               A backup is written to cheboygan_flood_map_v3.html.bak
"""

import math, json, time, urllib.request, urllib.parse, sys, os, re
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# RIVER CENTERLINE  — keep in sync with RCL in the HTML file
# ─────────────────────────────────────────────────────────────────────────────
RCL = [
    (45.636043, -84.479872),  # Dam
    (45.636129, -84.479714),  # 1
    (45.636902, -84.478522),  # 2
    (45.637447, -84.478719),  # 3
    (45.638824, -84.480401),  # 4
    (45.640748, -84.479436),  # 5
    (45.641706, -84.478311),  # 6
    (45.642148, -84.477203),  # 7
    (45.644716, -84.474496),  # 8
    (45.647133, -84.472054),  # 9
    (45.650048, -84.470226),  # 10
    (45.654327, -84.466446),  # 11
    (45.658660, -84.462432),  # 12
    (45.667612, -84.454337),  # Mouth
]

# ─────────────────────────────────────────────────────────────────────────────
# HYDRAULIC MODEL PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
# Water surface elevation at dam for each scenario (ft MSL)
# Lake Huron boundary condition: 581.0 ft MSL
# +0.5 ft snowmelt saturation correction applied to terrain threshold
SCENARIOS = {
    "partial": {
        "WSE_DAM": 590.5,    # 581.5 tailwater + 9 ft surge (partial breach head)
        "BC":      581.5,    # downstream boundary condition (Lake Huron + 0.5 sat)
        "GAMMA":   0.35,     # Manning's power-law attenuation exponent
        "Q_label": "20,000–35,000 cfs",
    },
    "full": {
        "WSE_DAM": 592.85,   # near full reservoir level (581.5 + 11.35 ft surge)
        "BC":      581.5,
        "GAMMA":   0.25,
        "Q_label": "50,000–100,000 cfs",
    },
}

# Inundation depth thresholds (ft above terrain) to define zone boundaries
ZONE_THRESHOLDS = {
    "z1": 4.0,   # Zone 1: 4+ ft — evacuate
    "z2": 2.0,   # Zone 2: 2–4 ft — high risk
    "z3": 0.5,   # Zone 3: 0.5–2 ft — watch
}

# Transect geometry
N_TRANSECTS  = 18      # transects along river (more = smoother polygon)
HALF_WIDTH   = 1200    # ft each side of centerline to sample
STEP_FT      = 100     # ft between sample points on each transect

# Coordinate conversion constants (at 45.65°N)
FTL  = 364566.0   # feet per degree latitude
FTLO = 255200.0   # feet per degree longitude

# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def rcl_length():
    total = 0.0
    for i in range(1, len(RCL)):
        dlat = (RCL[i][0] - RCL[i-1][0]) * FTL
        dlon = (RCL[i][1] - RCL[i-1][1]) * FTLO
        total += math.sqrt(dlat**2 + dlon**2)
    return total

def rcl_at(frac):
    """Return (lat, lon, tangent_lat, tangent_lon) at fractional distance along RCL."""
    segs = []
    total = 0.0
    for i in range(1, len(RCL)):
        dlat = (RCL[i][0] - RCL[i-1][0]) * FTL
        dlon = (RCL[i][1] - RCL[i-1][1]) * FTLO
        seg_len = math.sqrt(dlat**2 + dlon**2)
        segs.append(seg_len)
        total += seg_len
    target = min(frac * total, total)
    cum = 0.0
    for i, seg_len in enumerate(segs):
        if cum + seg_len >= target or i == len(segs) - 1:
            t = (target - cum) / seg_len if seg_len > 0 else 0.0
            lat = RCL[i][0] + t * (RCL[i+1][0] - RCL[i][0])
            lon = RCL[i][1] + t * (RCL[i+1][1] - RCL[i][1])
            dlat = (RCL[i+1][0] - RCL[i][0]) * FTL
            dlon = (RCL[i+1][1] - RCL[i][1]) * FTLO
            tl = math.sqrt(dlat**2 + dlon**2) or 1.0
            return lat, lon, dlat/tl, dlon/tl
        cum += seg_len
    return RCL[-1][0], RCL[-1][1], 0, 1

def transect_points(frac):
    """Generate sample points perpendicular to centerline at given fraction."""
    lat, lon, tn, te = rcl_at(frac)
    # Perpendicular direction (rotate tangent 90°)
    pn, pe = -te, tn
    n_steps = int(HALF_WIDTH / STEP_FT)
    pts = []
    for s in range(-n_steps, n_steps + 1):
        offset_ft = s * STEP_FT
        slat = lat + (pn * offset_ft) / FTL
        slon = lon + (pe * offset_ft) / FTLO
        pts.append((slat, slon, offset_ft))
    return pts

def wse_at_distance(scenario, frac):
    """Water surface elevation (ft MSL) at fractional distance from dam."""
    s = SCENARIOS[scenario]
    total_len = rcl_length()
    dist_ft = frac * total_len
    f = max(0.0, 1.0 - dist_ft / total_len)
    return s["BC"] + (s["WSE_DAM"] - s["BC"]) * (f ** s["GAMMA"])

# ─────────────────────────────────────────────────────────────────────────────
# USGS 3DEP ELEVATION QUERY
# ─────────────────────────────────────────────────────────────────────────────
EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
_elev_cache = {}

def query_elevation(lat, lon, retries=3, timeout=8):
    """Query USGS 3DEP EPQS for elevation at a single point. Returns ft MSL."""
    key = f"{lat:.6f},{lon:.6f}"
    if key in _elev_cache:
        return _elev_cache[key]
    params = urllib.parse.urlencode({
        "x": f"{lon:.6f}", "y": f"{lat:.6f}",
        "wkid": "4326", "units": "Feet", "includeDate": "false"
    })
    url = f"{EPQS_URL}?{params}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CheboyganFloodMap/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                val = float(data.get("value", -9999))
                if val < -9000:
                    return None
                _elev_cache[key] = val
                return val
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    return None

def query_all_points(all_pts):
    """Query elevations for all points using a thread pool. Returns dict key→elev."""
    print(f"  Querying {len(all_pts)} elevation points from USGS 3DEP...")
    results = {}
    failed = 0
    # Batch with max 8 concurrent threads (respectful to USGS API)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(query_elevation, lat, lon): (lat, lon)
            for lat, lon, _ in all_pts
        }
        done = 0
        for future in as_completed(futures):
            lat, lon = futures[future]
            key = f"{lat:.6f},{lon:.6f}"
            try:
                val = future.result()
                results[key] = val
                if val is None:
                    failed += 1
            except Exception:
                results[key] = None
                failed += 1
            done += 1
            if done % 20 == 0:
                print(f"    {done}/{len(all_pts)} complete ({failed} failed)...")
    print(f"  Done. {len(all_pts)-failed}/{len(all_pts)} elevations retrieved.")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# ZONE POLYGON COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────
def find_zone_boundary(transect_pts, wse, threshold, elev_data):
    """
    Find outermost flooded point on each side of centerline for a given
    depth threshold. Returns (pos_boundary_ll, neg_boundary_ll).
    pos = left side of river (positive offset), neg = right side.
    """
    center = next((p for p in transect_pts if p[2] == 0), transect_pts[len(transect_pts)//2])
    center_ll = (center[0], center[1])

    pos_pts = sorted([p for p in transect_pts if p[2] >= 0], key=lambda p: p[2])
    neg_pts = sorted([p for p in transect_pts if p[2] <= 0], key=lambda p: -p[2])

    def outermost(pts):
        best = center_ll
        for lat, lon, off in reversed(pts):
            key = f"{lat:.6f},{lon:.6f}"
            elev = elev_data.get(key)
            if elev is not None and (wse - elev) > threshold:
                best = (lat, lon)
                break
        return best

    return outermost(pos_pts), outermost(neg_pts)

def build_zone_polygon(transects, fracs, scenario, zone_key, elev_data):
    """Build a closed polygon for a zone by connecting boundary points across transects."""
    threshold = ZONE_THRESHOLDS[zone_key]
    pos_boundary = []
    neg_boundary = []
    for i, (t_pts, frac) in enumerate(zip(transects, fracs)):
        wse = wse_at_distance(scenario, frac)
        pb, nb = find_zone_boundary(t_pts, wse, threshold, elev_data)
        pos_boundary.append(pb)
        neg_boundary.append(nb)
    # Polygon: pos side forward + neg side reversed = closed ring
    polygon = pos_boundary + list(reversed(neg_boundary))
    return polygon

def smooth_polygon(pts, window=3):
    """Simple moving-average smoothing to reduce jaggedness."""
    n = len(pts)
    smoothed = []
    hw = window // 2
    for i in range(n):
        lats = [pts[(i+j-hw) % n][0] for j in range(window)]
        lons = [pts[(i+j-hw) % n][1] for j in range(window)]
        smoothed.append((sum(lats)/window, sum(lons)/window))
    return smoothed

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
def poly_to_js(pts, name):
    """Format polygon points as a JavaScript array."""
    coords = ",\n    ".join(f"[{lat:.6f},{lon:.6f}]" for lat, lon in pts)
    return f"const {name} = [\n    {coords}\n  ];"

def build_js_block(all_polys):
    """Build the complete PRE_COMPUTED_ZONES JavaScript block."""
    lines = ["// PRE_COMPUTED_ZONES_START",
             "// Generated by generate_terrain_zones.py — do not edit manually.",
             "// Re-run the script to update after changing RCL coordinates.",
             "const TERRAIN_ZONES = {"]
    for sc in ["partial", "full"]:
        lines.append(f"  {sc}: {{")
        for zone in ["z3", "z2", "z1"]:
            key = f"{sc}_{zone}"
            pts = all_polys[key]
            coords = ", ".join(f"[{lat:.6f},{lon:.6f}]" for lat, lon in pts)
            lines.append(f"    {zone}: [{coords}],")
        lines.append("  },")
    lines.append("};")
    lines.append("// PRE_COMPUTED_ZONES_END")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# HTML INJECTION
# ─────────────────────────────────────────────────────────────────────────────
ZONES_PLACEHOLDER = "// PRE_COMPUTED_ZONES_PLACEHOLDER"
ZONES_START_TAG   = "// PRE_COMPUTED_ZONES_START"
ZONES_END_TAG     = "// PRE_COMPUTED_ZONES_END"

USE_TERRAIN_PATCH = """
// TERRAIN ZONE RENDERING — uses pre-computed TERRAIN_ZONES instead of buf()
// Overrides the buffer-based render() for terrain-aware polygons.
const _orig_render = render;
function render(sc) {
  curSc = sc;
  ORD.forEach(id => { if (aL[id]) { map.removeLayer(aL[id]); delete aL[id]; } });
  const def = ZS[sc];
  // Use terrain-aware polygons for z1/z2/z3; buffer for river channel
  ORD.forEach(id => {
    const d = def[id];
    const pts = (id !== 'rv' && TERRAIN_ZONES[sc] && TERRAIN_ZONES[sc][id])
      ? TERRAIN_ZONES[sc][id]
      : buf(RCL, BF[sc][id]);
    if (!pts || pts.length < 3) return;
    const poly = L.polygon(pts, {
      color:d.c, fillColor:d.fc||d.c, fillOpacity:d.fo,
      weight:d.w, opacity:.90, dashArray:d.d
    }).addTo(map);
    poly.on('click',  e => L.popup({maxWidth:320}).setLatLng(e.latlng).setContent(POP[sc][id]).openOn(map));
    poly.on('mouseover', function(){this.setStyle({fillOpacity:Math.min(1,d.fo+.12)});});
    poly.on('mouseout',  function(){this.setStyle({fillOpacity:d.fo});});
    if (!vis[id]) map.removeLayer(poly);
    aL[id] = poly;
  });
  buildZL(sc);
  const dn = document.getElementById('scd');
  dn.className = 'sn '+(sc==='full'?'full':'partial');
  dn.innerHTML = SCND[sc];
}
"""

def inject_into_html(html_path, js_block):
    """Inject pre-computed zones into the HTML file."""
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Write backup
    backup = html_path + ".bak"
    with open(backup, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Backup written: {backup}")

    # Remove old terrain zones block if present
    if ZONES_START_TAG in html and ZONES_END_TAG in html:
        pattern = re.compile(
            re.escape(ZONES_START_TAG) + r".*?" + re.escape(ZONES_END_TAG),
            re.DOTALL
        )
        html = pattern.sub(ZONES_PLACEHOLDER, html)

    # Inject new terrain zones block before </script>
    if ZONES_PLACEHOLDER in html:
        html = html.replace(ZONES_PLACEHOLDER, js_block)
    else:
        # Insert before the last </script> tag
        html = html.replace("</script>", js_block + "\n" + USE_TERRAIN_PATCH + "\n</script>", 1)

    # Also inject USE_TERRAIN_PATCH if not already present
    if "TERRAIN ZONE RENDERING" not in html:
        html = html.replace("</script>", USE_TERRAIN_PATCH + "\n</script>", 1)

    # Update model description in panel to note terrain-aware zones
    html = html.replace(
        "Manning's Buffer Method — Static Centerline",
        "Manning's WSE + USGS 3DEP Terrain (pre-computed)"
    )
    html = html.replace(
        "Manning's buffer zones · static centerline · instant render",
        "Manning's WSE + USGS 3DEP terrain · pre-computed · instant render"
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML updated: {html_path}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    html_path = os.path.join(os.path.dirname(__file__), "cheboygan_flood_map_v3.html")
    if not os.path.exists(html_path):
        print(f"ERROR: Cannot find {html_path}")
        print("Make sure this script is in the same folder as cheboygan_flood_map_v3.html")
        sys.exit(1)

    print("=" * 60)
    print("Cheboygan Flood Map — Terrain Zone Generator")
    print("=" * 60)

    # Step 1: Build transect sample points
    print("\nStep 1/4  Generating transects...")
    fracs = [i / (N_TRANSECTS - 1) for i in range(N_TRANSECTS)]
    transects = [transect_points(f) for f in fracs]
    all_pts_flat = [pt for t in transects for pt in t]
    # Deduplicate (some points may overlap near centerline)
    seen = set()
    unique_pts = []
    for pt in all_pts_flat:
        key = f"{pt[0]:.6f},{pt[1]:.6f}"
        if key not in seen:
            seen.add(key)
            unique_pts.append(pt)
    print(f"  {N_TRANSECTS} transects × {len(transects[0])} points = {len(unique_pts)} unique sample points")

    # Step 2: Query USGS 3DEP elevations
    print("\nStep 2/4  Querying USGS 3DEP elevations...")
    print("  (This takes ~30–60 seconds — please wait)")
    elev_data = query_all_points(unique_pts)

    # Check coverage
    valid = sum(1 for v in elev_data.values() if v is not None)
    coverage = valid / len(elev_data) * 100
    print(f"  Coverage: {valid}/{len(elev_data)} points ({coverage:.0f}%)")
    if coverage < 60:
        print("  WARNING: Low elevation coverage. Zones may be inaccurate.")
        print("  Check your internet connection and try again.")

    # Step 3: Compute zone polygons
    print("\nStep 3/4  Computing terrain-aware zone polygons...")
    all_polys = {}
    for sc in ["partial", "full"]:
        for zone in ["z3", "z2", "z1"]:
            key = f"{sc}_{zone}"
            pts = build_zone_polygon(transects, fracs, sc, zone, elev_data)
            pts = smooth_polygon(pts, window=3)
            all_polys[key] = pts
            print(f"  {key}: {len(pts)} polygon vertices")

    # Step 4: Inject into HTML
    print("\nStep 4/4  Writing pre-computed zones into HTML...")
    js_block = build_js_block(all_polys)
    inject_into_html(html_path, js_block)

    print("\n" + "=" * 60)
    print("Done! cheboygan_flood_map_v3.html has been updated.")
    print("Flood zones now follow actual terrain elevation.")
    print("The map still loads instantly — no runtime API calls.")
    print("=" * 60)

    # Print elevation summary for key transects
    print("\nElevation summary (ft MSL) at selected transect centers:")
    for i, frac in enumerate([0.0, 0.25, 0.5, 0.75, 1.0]):
        lat, lon, _, _ = rcl_at(frac)
        key = f"{lat:.6f},{lon:.6f}"
        elev = elev_data.get(key, "N/A")
        wse_p = wse_at_distance("partial", frac)
        wse_f = wse_at_distance("full", frac)
        label = ["Dam", "25%", "50%", "75%", "Mouth"][i]
        print(f"  {label:6s}  terrain={elev if isinstance(elev,str) else f'{elev:.1f} ft':>10}  "
              f"WSE partial={wse_p:.1f}  WSE full={wse_f:.1f}")

if __name__ == "__main__":
    main()
