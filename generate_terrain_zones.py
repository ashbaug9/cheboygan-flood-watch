#!/usr/bin/env python3
"""
generate_terrain_zones.py
─────────────────────────────────────────────────────────────────────────────
Queries USGS 3DEP elevation for the Cheboygan River corridor, computes
terrain-aware flood zone boundary polygons for partial and full breach
scenarios, and writes pre-computed coordinates into the HTML map.

Primary source:  USGS 3DEP ImageServer getSamples API
                 1m LiDAR-derived DEM where available (Cheboygan 2019 coverage)
                 Returns NAVD88 meters directly — no geoid conversion needed
                 Batch requests (50 pts each) — fast, no compilation required
Fallback:        USGS EPQS point query service

Requirements:    Python 3.8+ standard library only — NO pip installs needed

Colab usage:
    1. Upload generate_terrain_zones.py and cheboygan_flood_map_v3.html
    2. Run:  !python generate_terrain_zones.py
    3. Download updated cheboygan_flood_map_v3.html

Output:  cheboygan_flood_map_v3.html updated in-place
         cheboygan_flood_map_v3.html.bak  (automatic backup)
"""

import math, json, sys, os, re, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# ELEVATION API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
# Primary: 3DEP ImageServer — batch up to 50 points, 1m LiDAR-derived, NAVD88 metres
IMAGESERVER_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer/getSamples"
)
# Fallback: EPQS point query
EPQS_URL = "https://epqs.nationalmap.gov/v1/json"

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
# WSE back-calculated from USGS 04132052:
#   Apr 13 7am: 595.2ft NAVD88, officials confirmed 13.75" below crest
#   Crest = 595.2 + (13.75/12) = 596.346ft NAVD88
SCENARIOS = {
    "partial": {
        "WSE_DAM": 597.5,  # crest + ~1.15ft surge (overtopping/erosional failure)
        "BC":      581.5,  # Lake Huron + 0.5ft snowmelt saturation
        "GAMMA":   0.35,   # Manning's power-law attenuation exponent
        "Q_label": "20,000–35,000 cfs",
    },
    "full": {
        "WSE_DAM": 599.0,  # crest + ~2.65ft (near full reservoir head)
        "BC":      581.5,
        "GAMMA":   0.25,
        "Q_label": "50,000–100,000 cfs",
    },
}

ZONE_THRESHOLDS = {
    "z1": 4.0,   # Zone 1: 4+ ft — evacuate
    "z2": 2.0,   # Zone 2: 2–4 ft — high risk
    "z3": 0.5,   # Zone 3: 0.5–2 ft — watch
}

# Transect geometry
N_TRANSECTS = 30      # transects along river
HALF_WIDTH  = 1200    # ft each side of centerline
STEP_FT     = 75      # ft between sample points per transect

# Coordinate conversion at 45.65°N
FTL  = 364566.0   # feet per degree latitude
FTLO = 255200.0   # feet per degree longitude

# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def rcl_length():
    total = 0.0
    for i in range(1, len(RCL)):
        dl = (RCL[i][0]-RCL[i-1][0])*FTL
        dg = (RCL[i][1]-RCL[i-1][1])*FTLO
        total += math.sqrt(dl**2+dg**2)
    return total

def rcl_at(frac):
    segs, total = [], 0.0
    for i in range(1, len(RCL)):
        dl=(RCL[i][0]-RCL[i-1][0])*FTL; dg=(RCL[i][1]-RCL[i-1][1])*FTLO
        sl=math.sqrt(dl**2+dg**2); segs.append(sl); total+=sl
    target=min(frac*total,total); cum=0.0
    for i,sl in enumerate(segs):
        if cum+sl>=target or i==len(segs)-1:
            t=(target-cum)/sl if sl>0 else 0.0
            lat=RCL[i][0]+t*(RCL[i+1][0]-RCL[i][0])
            lon=RCL[i][1]+t*(RCL[i+1][1]-RCL[i][1])
            dl=(RCL[i+1][0]-RCL[i][0])*FTL; dg=(RCL[i+1][1]-RCL[i][1])*FTLO
            tl=math.sqrt(dl**2+dg**2) or 1.0
            return lat,lon,dl/tl,dg/tl
        cum+=sl
    return RCL[-1][0],RCL[-1][1],0,1

def transect_points(frac):
    lat,lon,tn,te=rcl_at(frac); pn,pe=-te,tn
    n_steps=int(HALF_WIDTH/STEP_FT); pts=[]
    for s in range(-n_steps,n_steps+1):
        off=s*STEP_FT
        pts.append((lat+(pn*off)/FTL, lon+(pe*off)/FTLO, off))
    return pts

def wse_at_distance(scenario, frac):
    s=SCENARIOS[scenario]; total=rcl_length()
    f=max(0.0,1.0-frac*total/total)
    return s["BC"]+(s["WSE_DAM"]-s["BC"])*(f**s["GAMMA"])

# ─────────────────────────────────────────────────────────────────────────────
# ELEVATION QUERIES
# ─────────────────────────────────────────────────────────────────────────────
_cache = {}   # key="lat,lon" → elevation ft NAVD88

def _batch_imageserver(points, timeout=20):
    """
    Query USGS 3DEP ImageServer for up to 50 points in one request.
    Returns dict: index → elevation ft NAVD88, or None on failure.
    points: list of (lat, lon) tuples
    """
    geom = json.dumps({
        "points": [[lon, lat] for lat, lon in points],
        "spatialReference": {"wkid": 4326}
    })
    params = urllib.parse.urlencode({
        "geometry":             geom,
        "geometryType":         "esriGeometryMultipoint",
        "returnFirstValueOnly": "true",
        "interpolation":        "RSP_BilinearInterpolation",
        "f":                    "json"
    })
    url = IMAGESERVER_URL + "?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "CheboyganFloodMap/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    results = {}
    for s in data.get("samples", []):
        idx = int(s["locationId"])
        try:
            val_m = float(s["value"])
            # ImageServer returns NAVD88 metres — convert to feet
            results[idx] = val_m * 3.28084
        except (ValueError, KeyError):
            results[idx] = None
    return results

def _single_epqs(lat, lon, timeout=10, retries=3):
    """Single-point EPQS fallback. Returns ft NAVD88 or None."""
    url = (f"{EPQS_URL}?x={lon:.6f}&y={lat:.6f}"
           "&wkid=4326&units=Feet&includeDate=false")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CheboyganFloodMap/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            val = float(data.get("value", -9999))
            if val > -9000:
                return val
        except Exception:
            time.sleep(0.4 * (2**attempt))
    return None

def query_all_points(all_pts):
    """
    Phase 1: Batch ImageServer requests (50 pts each, threaded).
    Phase 2: EPQS retry for any failures.
    Returns dict key="lat,lon" → ft NAVD88.
    """
    total = len(all_pts)
    print(f"  Source: USGS 3DEP ImageServer (1m LiDAR-derived, NAVD88)")
    print(f"  Querying {total} points in batches of 50...")

    # Build ordered list to preserve index→key mapping
    pts_list = [(lat, lon) for lat, lon, _ in all_pts]
    keys     = [f"{lat:.6f},{lon:.6f}" for lat, lon in pts_list]

    # Split into batches of 50
    BATCH = 50
    batches = [pts_list[i:i+BATCH] for i in range(0, total, BATCH)]

    results = {}   # index → value
    failed  = []

    def run_batch(batch_idx, batch):
        offset = batch_idx * BATCH
        try:
            res = _batch_imageserver(batch, timeout=25)
            out = {}
            for j, (lat, lon) in enumerate(batch):
                global_idx = offset + j
                val = res.get(j)
                out[global_idx] = val
                if val is None:
                    failed.append(global_idx)
            return out
        except Exception as e:
            # Whole batch failed — mark all as None for retry
            out = {}
            for j in range(len(batch)):
                global_idx = offset + j
                out[global_idx] = None
                failed.append(global_idx)
            return out

    # Run batches with 4 threads
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_batch, i, b): i
                   for i, b in enumerate(batches)}
        done = 0
        for future in as_completed(futures):
            results.update(future.result())
            done += BATCH
            pct = min(done, total)
            fails = sum(1 for v in results.values() if v is None)
            print(f"    {pct}/{total}  —  {fails} failed so far")

    # Phase 2: EPQS retry for failures
    fail_idxs = [i for i, v in results.items() if v is None]
    if fail_idxs:
        print(f"\n  Phase 2: EPQS retry for {len(fail_idxs)} failed points...")
        still_failed = 0
        for n, idx in enumerate(fail_idxs):
            lat, lon = pts_list[idx]
            time.sleep(0.25)
            val = _single_epqs(lat, lon)
            results[idx] = val
            if val is None:
                still_failed += 1
            if (n+1) % 20 == 0:
                print(f"    Retry {n+1}/{len(fail_idxs)}  —  {still_failed} still failing")
        recovered = len(fail_idxs) - still_failed
        print(f"  Phase 2 done: recovered {recovered}/{len(fail_idxs)}")

    # Build final key→value dict
    final = {keys[i]: results.get(i) for i in range(total)}
    valid = sum(1 for v in final.values() if v is not None)
    print(f"\n  Final coverage: {valid}/{total} ({valid/total*100:.0f}%)")
    if valid/total < 0.7:
        print("  ⚠ Coverage below 70% — some zones may use buffer fallback")
    return final

# ─────────────────────────────────────────────────────────────────────────────
# ZONE POLYGON COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────
def find_zone_boundary(transect_pts, wse, threshold, elev_data):
    center = next((p for p in transect_pts if p[2]==0), transect_pts[len(transect_pts)//2])
    center_ll = (center[0], center[1])
    pos = sorted([p for p in transect_pts if p[2]>=0], key=lambda p: p[2])
    neg = sorted([p for p in transect_pts if p[2]<=0], key=lambda p: -p[2])
    def outermost(pts):
        best = center_ll
        for lat,lon,off in reversed(pts):
            key = f"{lat:.6f},{lon:.6f}"
            elev = elev_data.get(key)
            if elev is not None and (wse-elev)>threshold:
                best=(lat,lon); break
        return best
    return outermost(pos), outermost(neg)

def build_zone_polygon(transects, fracs, scenario, zone_key, elev_data):
    threshold=ZONE_THRESHOLDS[zone_key]; pb,nb=[],[]
    for t_pts,frac in zip(transects,fracs):
        wse=wse_at_distance(scenario,frac)
        p,n=find_zone_boundary(t_pts,wse,threshold,elev_data)
        pb.append(p); nb.append(n)
    return pb+list(reversed(nb))

def smooth_polygon(pts, window=3):
    n=len(pts); hw=window//2; smoothed=[]
    for i in range(n):
        lats=[pts[(i+j-hw)%n][0] for j in range(window)]
        lons=[pts[(i+j-hw)%n][1] for j in range(window)]
        smoothed.append((sum(lats)/window,sum(lons)/window))
    return smoothed

# ─────────────────────────────────────────────────────────────────────────────
# HTML INJECTION
# ─────────────────────────────────────────────────────────────────────────────
ZONES_START = "// PRE_COMPUTED_ZONES_START"
ZONES_END   = "// PRE_COMPUTED_ZONES_END"

USE_TERRAIN_PATCH = """
// TERRAIN ZONE RENDERING — uses pre-computed TERRAIN_ZONES instead of buf()
const _orig_render = render;
function render(sc) {
  curSc = sc;
  ORD.forEach(id => { if (aL[id]) { map.removeLayer(aL[id]); delete aL[id]; } });
  const def = ZS[sc];
  ORD.forEach(id => {
    const d = def[id];
    const pts = (id !== 'rv' && TERRAIN_ZONES[sc] && TERRAIN_ZONES[sc][id])
      ? TERRAIN_ZONES[sc][id] : buf(RCL, BF[sc][id]);
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

def build_js_block(all_polys):
    lines = [
        ZONES_START,
        "// Generated by generate_terrain_zones.py",
        "// Source: USGS 3DEP ImageServer (1m LiDAR-derived, NAVD88)",
        f"// Transects: {N_TRANSECTS} | Step: {STEP_FT}ft | Half-width: {HALF_WIDTH}ft",
        "// Do not edit manually — re-run the script to update.",
        "const TERRAIN_ZONES = {"
    ]
    for sc in ["partial","full"]:
        lines.append(f"  {sc}: {{")
        for zone in ["z3","z2","z1"]:
            pts=all_polys[f"{sc}_{zone}"]
            coords=", ".join(f"[{lat:.6f},{lon:.6f}]" for lat,lon in pts)
            lines.append(f"    {zone}: [{coords}],")
        lines.append("  },")
    lines.append("};")
    lines.append(ZONES_END)
    return "\n".join(lines)

def inject_into_html(html_path, js_block):
    with open(html_path,"r",encoding="utf-8") as f: html=f.read()
    with open(html_path+".bak","w",encoding="utf-8") as f: f.write(html)
    print(f"  Backup: {html_path}.bak")
    if ZONES_START in html and ZONES_END in html:
        pattern=re.compile(re.escape(ZONES_START)+r".*?"+re.escape(ZONES_END),re.DOTALL)
        html=pattern.sub("// PRE_COMPUTED_ZONES_PLACEHOLDER",html)
    if "// PRE_COMPUTED_ZONES_PLACEHOLDER" in html:
        html=html.replace("// PRE_COMPUTED_ZONES_PLACEHOLDER",js_block)
    else:
        html=html.replace("</script>",js_block+"\n"+USE_TERRAIN_PATCH+"\n</script>",1)
    if "TERRAIN ZONE RENDERING" not in html:
        html=html.replace("</script>",USE_TERRAIN_PATCH+"\n</script>",1)
    for old,new in [
        ("Manning's buffer zones · static centerline · instant render",
         "USGS 3DEP 1m LiDAR terrain · pre-computed · instant render"),
        ("USGS LiDAR 2019 terrain · pre-computed · instant render",
         "USGS 3DEP 1m LiDAR terrain · pre-computed · instant render"),
        ("Manning's WSE + USGS 3DEP terrain · pre-computed · instant render",
         "USGS 3DEP 1m LiDAR terrain · pre-computed · instant render"),
        ("Manning's Buffer Method — Static Centerline",
         "Manning's WSE + USGS 3DEP 1m LiDAR (ImageServer)"),
        ("Manning's WSE + USGS 3DEP Terrain (pre-computed)",
         "Manning's WSE + USGS 3DEP 1m LiDAR (ImageServer)"),
        ("Manning's WSE + USGS LiDAR 2019 (MI_FEMA_Cheboygan)",
         "Manning's WSE + USGS 3DEP 1m LiDAR (ImageServer)"),
    ]:
        html=html.replace(old,new)
    with open(html_path,"w",encoding="utf-8") as f: f.write(html)
    print(f"  HTML updated: {html_path}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    html_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cheboygan_flood_map_v3.html")
    if not os.path.exists(html_path):
        print(f"ERROR: Cannot find {html_path}")
        print("Make sure this script is in the same folder as cheboygan_flood_map_v3.html")
        sys.exit(1)

    print("="*60)
    print("Cheboygan Flood Map — Terrain Zone Generator v3")
    print("No dependencies beyond Python standard library.")
    print("="*60)

    print("\nStep 1/4  Generating transects...")
    fracs=[i/(N_TRANSECTS-1) for i in range(N_TRANSECTS)]
    transects=[transect_points(f) for f in fracs]
    seen,unique_pts=set(),[]
    for t in transects:
        for pt in t:
            key=f"{pt[0]:.6f},{pt[1]:.6f}"
            if key not in seen: seen.add(key); unique_pts.append(pt)
    print(f"  {N_TRANSECTS} transects × {len(transects[0])} pts = {len(unique_pts)} unique sample points")

    print(f"\nStep 2/4  Querying ground elevations...")
    print(f"  (Using 3DEP ImageServer — expect 60–90 seconds)")
    t0=time.time()
    elev_data=query_all_points(unique_pts)
    print(f"  Completed in {time.time()-t0:.0f}s")

    print("\nStep 3/4  Computing terrain-aware zone polygons...")
    all_polys={}
    for sc in ["partial","full"]:
        for zone in ["z3","z2","z1"]:
            key=f"{sc}_{zone}"
            pts=build_zone_polygon(transects,fracs,sc,zone,elev_data)
            pts=smooth_polygon(pts,window=3)
            all_polys[key]=pts
            print(f"  {key}: {len(pts)} vertices")

    print("\nStep 4/4  Writing terrain zones into HTML...")
    inject_into_html(html_path, build_js_block(all_polys))

    print("\n"+"="*60)
    print("Done! cheboygan_flood_map_v3.html updated.")
    print(f"Zones: {N_TRANSECTS} transects, {STEP_FT}ft step, {HALF_WIDTH}ft corridor")
    print("Map loads instantly — terrain data is pre-baked.")
    print("="*60)

    print("\nGround elevation at centerline (NAVD88):")
    for label,frac in [("Dam",0.0),("25%",0.25),("50%",0.5),("75%",0.75),("Mouth",1.0)]:
        lat,lon,_,_=rcl_at(frac)
        key=f"{lat:.6f},{lon:.6f}"
        elev=elev_data.get(key)
        wse_p=wse_at_distance("partial",frac)
        wse_f=wse_at_distance("full",frac)
        e=f"{elev:.1f}ft" if elev else "N/A"
        print(f"  {label:5s}  terrain={e:>10}  WSE partial={wse_p:.1f}ft  WSE full={wse_f:.1f}ft")

if __name__=="__main__":
    main()
