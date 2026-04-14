# Cheboygan Dam Flood Prediction Model
## Technical Description & Caveats

**Prepared:** April 13, 2026
**Status:** Active Emergency Reference — READY Alert
**Model version:** v3 (Manning's WSE + USGS 3DEP terrain)

---

## What This Model Does

This tool produces a **terrain-aware flood inundation estimate** for two breach scenarios at the Cheboygan Lock & Dam. It is designed to answer one question: *if the dam fails, which areas are likely to flood, how deep, and how quickly?*

It does this in three steps:

1. **Compute a water surface elevation (WSE) profile** along the 2.4-mile river reach from the dam to Lake Huron, using Manning's equation and established dam-break hydraulics
2. **Query actual ground elevation** at 615 points distributed across perpendicular transects of the reach, using the USGS 3D Elevation Program (3DEP) national dataset
3. **Compare water surface to ground** at every sampled point — areas where the water surface exceeds the ground elevation by the zone threshold are included in that zone

Zone boundaries are therefore not uniform rings around the river. They follow the actual topography of the city, narrowing where ground rises and widening where terrain is low.

---

## Hydraulic Model

### Breach Scenarios

Two scenarios are modeled, both derived from **Froehlich (1995)** empirical dam-break equations, the same methodology used in FEMA P-946 simplified breach analysis:

| Parameter | Partial Breach | Full Breach |
|---|---|---|
| Failure mode | Progressive overtopping / erosional failure | Instantaneous complete structural failure |
| Peak discharge (Qp) | 17,500 cfs | 25,000 cfs |
| Normal spillway capacity | 7,640 cfs | 7,640 cfs |
| WSE at dam (surge + saturation) | 590.0 ft MSL | 593.35 ft MSL |
| Downstream boundary | 581.5 ft MSL (Lake Huron + sat.) | 581.5 ft MSL |
| Attenuation exponent γ | 0.35 | 0.25 |

### Water Surface Profile

The water surface elevation at any point along the reach is calculated using a **power-law attenuation** function derived from Manning's friction on a nearly-flat, lake-controlled channel:

```
WSE(d) = WSE_bc + (WSE_dam − WSE_bc) × (1 − d/L)^γ
```

Where:
- `d` = distance from dam (ft)
- `L` = total reach length (~12,794 ft / 2.42 miles)
- `WSE_bc` = Lake Huron boundary condition (581.5 ft MSL with saturation)
- `γ` = attenuation exponent (0.35 partial, 0.25 full)

The attenuation exponent is calibrated to Manning's equation for the Cheboygan city reach using:
- Channel width: 80 ft (USACE maintained navigation channel)
- Channel depth at bankfull: 7 ft
- Manning's n (channel): 0.030 (maintained engineered channel)
- Manning's n (floodplain): 0.080 (urban — buildings, streets, lawns)
- Channel slope: 0.0001 ft/ft (nearly flat; lake-controlled downstream boundary)
- Normal channel capacity at these parameters: ~914 cfs

A partial breach at 17,500 cfs represents **19× the normal channel capacity**. A full breach at 25,000 cfs represents **27× normal capacity**. Both scenarios produce immediate and widespread overbank flooding.

### Saturation Correction

A **+0.5 ft additive correction** is applied uniformly to the WSE profile to account for current snowmelt conditions. Saturated soils have near-zero infiltration capacity, meaning flood water does not drain into the ground but instead spreads laterally across the surface and remains standing. This correction is conservative by design — it slightly expands the inundation extent to reflect real-world conditions on April 13, 2026.

### Zone Depth Thresholds

| Zone | Inundation Depth | Meaning |
|---|---|---|
| Zone 1 — Evacuate | > 4.0 ft above ground | Life-threatening. Vehicles swept away. Structural damage. No safe shelter at ground level. |
| Zone 2 — High Risk | 2.0 – 4.0 ft | Vehicles impassable. Significant structural damage. Dangerous for adults. |
| Zone 3 — Watch | 0.5 – 2.0 ft | Dangerous for children, elderly, mobility-limited persons. Vehicles may stall. |

### Channel Geometry

The Cheboygan River city reach is a USACE-maintained navigation channel. Channel dimensions are derived from:
- USACE Detroit District navigation records
- Michigan DNR lock specifications (16 ft wide × 75 ft long vessels, 5 ft draft)
- Fishweb.com Michigan Inland Waterway navigation markers (GPS-confirmed waypoints)
- USGS hydraulic geometry regression for Southern Lower Michigan Ecoregion (DA = 1,455 sq mi): bankfull width ~200 ft (geomorphic floodplain), bankfull depth ~5 ft

### Arrival Time Estimates

Arrival times are derived from the Ritter (1892) dam-break wave celerity formula:

```
c₀ = √(g × h₀)  where h₀ = initial water depth behind dam
```

At current reservoir head (~12.5 ft): c₀ ≈ 20 ft/s (13.6 mph)

| Zone | Partial Breach | Full Breach |
|---|---|---|
| Zone 1 (0–300 ft) | 0–15 minutes | 0–4 minutes |
| Zone 2 (300–800 ft) | 30–90 minutes | 2–8 minutes |
| Zone 3 (up to 0.4 mi) | 1.5–3 hours | 5–20 minutes |

Full breach arrival times are dramatically shorter because the wave front carries more energy and the water surface is driven close to reservoir level across much of the reach.

---

## Elevation Data

Ground elevation at each sampled point is queried from the **USGS 3D Elevation Program (3DEP)** via the Elevation Point Query Service (EPQS) API at `epqs.nationalmap.gov`. The 3DEP dataset provides 1-meter resolution LiDAR-derived elevation for the Cheboygan area, referenced to NAVD88. Results are converted and used in the same datum as the hydraulic model (consistent with IGLD85 at Lake Huron elevations).

- **Transects:** 15 perpendicular cross-sections along the reach (desktop), 10 (mobile)
- **Points per transect:** 41 points spaced every 75 ft, extending 1,500 ft each side of the channel
- **Total elevation queries:** ~615 (desktop) / ~410 (mobile)
- **Caching:** Results are stored in browser localStorage for 7 days, keyed to the dam GPS coordinates. Subsequent page loads render zones instantly without re-querying.

---

## River Centerline

The river centerline (RCL) used to position transects is sourced from two inputs:

1. **GPS-confirmed anchors** from fishweb.com Michigan Inland Waterway navigation markers:
   - Dam: N45°38.164′ W84°28.777′ (verified against Google Maps satellite)
   - Flow projection point: N45°36.372′ W84°47.933′ (55.4° NE bearing, Google Maps confirmed)
   - State Street access: N45°38.678′ W84°28.469′
   - River mouth: N45°40.01′ W84°27.22′

2. **OpenStreetMap Overpass API** — fetched at page load from `overpass-api.de`. OSM contributors have precisely traced the Cheboygan River centerline from aerial imagery, producing the same centerline visible as a dotted line in Google's satellite and hybrid views. If the OSM fetch succeeds, it replaces the interpolated fallback RCL while preserving the GPS-confirmed anchors at the dam and mouth.

---

## What This Model Does NOT Do

**The following limitations are important and users should understand them before acting on this information.**

### 1. Static inundation — not time-stepped
The model shows peak worst-case inundation extent. It does not simulate the flood wave moving through the city in time steps. The zone boundaries represent the *maximum extent* the flood could reach at peak discharge — not where the water is at any given minute after breach.

### 2. No structure modeling
Buildings, levees, berms, road embankments, and retaining walls are not modeled. In reality these features deflect, channel, and block flood water in ways that the terrain elevation alone does not capture. A building may prevent flooding on its sheltered side even if the model places it inside Zone 1. Conversely, a walled parking lot may trap water and increase depths beyond the model's estimate.

### 3. No storm drain or sewer modeling
The storm drain network is not modeled. During a major breach event storm drains may act as conduits, carrying water to areas upstream of what terrain elevation alone would predict.

### 4. No rainfall contribution to the hydraulic model
Current rainfall (forecast 1–2.5" Monday April 13) is not added to the breach discharge. The saturation correction (+0.5 ft) partially accounts for pre-existing soil moisture reducing infiltration, but the runoff volume from Monday's rain is not included in Qp.

### 5. Simplified 1D hydraulic model
This is a simplified steady-state 1D model, not a full unsteady 2D hydraulic simulation (HEC-RAS 2D or equivalent). It does not model:
- Secondary flow effects at bends
- Backwater effects from tributaries (Black River enters approximately 3.5 miles south of the city)
- Bridge constrictions (US-23 bascule bridge, Lincoln Ave bridge create hydraulic chokepoints)
- Flow splitting into streets and returning to the channel

### 6. Elevation datum consistency
The hydraulic model uses IGLD85 lake level data (Lake Huron = 581.0 ft). USGS 3DEP elevations are NAVD88. The difference between IGLD85 and NAVD88 at this location is approximately 0.5–1.0 ft. This offset is partially absorbed by the saturation correction but introduces a systematic uncertainty of roughly ±0.5 ft in the absolute inundation depths.

### 7. RCL accuracy affects zone shape
The flood zone polygons are built as perpendicular transects from the river centerline. If the centerline deviates from the actual channel (particularly if the OSM fetch fails and the interpolated fallback is used), transects may be misaligned, producing zone boundaries that are shifted laterally. The Calibrate Channel tool in the desktop map allows manual correction.

### 8. Not a regulatory flood hazard determination
This model is not a FEMA Flood Insurance Study, does not constitute a Letter of Map Revision (LOMR), and does not supersede the official FEMA Flood Insurance Rate Map (FIRM) for Cheboygan County. It is an emergency planning reference only.

---

## Comparison to City Safety Zone Map

The City of Cheboygan released a Safety Zone map on April 10, 2026 showing a narrow corridor (one to two blocks) along the river as the cautionary zone. That map reflects **current riverbank overflow risk** from elevated water levels under active mitigation — not a dam breach scenario. The FEMA Special Flood Hazard Area for a maintained USACE navigation channel in flat urban terrain is inherently narrow.

This model addresses a different and more severe question: **what happens if the dam structure fails?** The wider zones in this model are not in conflict with the city's map — they answer different questions.

---

## Data Sources

| Source | Use |
|---|---|
| Froehlich (1995) | Breach discharge regression equations |
| FEMA P-946 | Simplified breach analysis methodology |
| Ritter (1892) | Dam-break wave celerity |
| Manning (1891) | Open-channel flow equation |
| USGS 3DEP / EPQS | Ground elevation at transect points |
| USGS NWIS (gage 04130000) | Live gage height, Cheboygan River near Cheboygan MI |
| USACE Detroit District | Channel geometry, navigation records |
| Michigan DNR | Lock specifications, dam inspection records |
| FERC dam registry | Dam height, hydraulic head, spillway capacity, reservoir volume |
| fishweb.com (MI Inland Waterway) | GPS-confirmed on-river navigation waypoints |
| OpenStreetMap Overpass API | River centerline geometry |
| Wikipedia / NWS / MSP EMHSD | Supplementary reach characteristics |

---

## Disclaimer

This model is produced as an emergency planning reference under active conditions. It has not been reviewed, certified, or endorsed by Cheboygan County Emergency Management, the Michigan Department of Natural Resources, FEMA, FERC, or the U.S. Army Corps of Engineers.

**Always follow official evacuation orders from Cheboygan County Emergency Management. In immediate danger, call 911.**

The Ready/Set/Go trigger levels (READY at 12", SET at 6", GO at 1" below crest) are the official thresholds established by the emergency management authority — not derived from this model.
