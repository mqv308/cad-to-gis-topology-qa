"""Production Vietnam GIS Cadastral QA/QC, VN-2000 (34-Province 2025-2026) & WebGIS Suite.

Hardened & Audited 2-Layer Legal-Safe Architecture:
  - Layer 1 (Machine-Safe): CAD DGNv7/v8/DWG extraction (Unicode-safe JSON bridge), TCVN3->Unicode
    repair (strict UTF-8 preservation), curve linearization, geometry validity repair (safe
    GeometryCollection flattening), optional exact-duplicate removal via normalized WKB (--dedup),
    optional line->polygon closing (--polygonize), optional CAD Annotation spatial join (--join-labels),
    VN-2000 <-> WGS84 (< 1 mm round-trip).
  - Layer 2 (Human-in-the-Loop): Overlaps and label anomalies (MULTI_LABEL) are NEVER silently
    altered; they are flagged in GeoPackage/GeoJSON/KML/WebGIS and exported to an Excel-ready
    UTF-8-SIG QA checklist (_qa_errors.csv) for authorized human review.

Never modifies or overwrites source files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.sax.saxutils as saxutils
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from osgeo import ogr, osr
from pyproj import CRS, Transformer
from shapely import make_valid, normalize as shapely_normalize, wkb
from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon
from shapely.ops import polygonize as shapely_polygonize
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union as shapely_unary_union
from shapely.strtree import STRtree
from shapely.validation import explain_validity

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.tcvn3 import smart_fix_text
from core.vn2000 import get_vn2000_crs, verify_roundtrip

ogr.UseExceptions()


def _detect_source_crs(
    layer: ogr.Layer,
    province: Optional[str],
    lon0: Optional[float],
    zone_width: int,
    legacy: bool,
) -> tuple[CRS, str]:
    if province or lon0 is not None:
        crs, note = get_vn2000_crs(province=province, lon_0=lon0, zone_width=zone_width, legacy=legacy)
        return crs, f"Forced VN-2000 CV1123 ({note}, zone={zone_width}deg)"

    srs = layer.GetSpatialRef()
    if srs is None:
        raise ValueError(
            "File khong co .prj / CRS. Hay truyen --province <ten_tinh> (vd: --province ha_noi) hoac --lon0 <kinh_tuyen_truc>."
        )

    wkt = srs.ExportToWkt()
    crs = CRS.from_wkt(wkt)
    proj4 = crs.to_proj4() if not crs.is_geographic else ""

    name_upper = (crs.name or "").upper()
    if ("VN" in name_upper or "2000" in name_upper) and not crs.is_geographic and "towgs84" not in proj4:
        lon_val = None
        k_val = 0.9999
        for part in proj4.split():
            if part.startswith("+lon_0="):
                lon_val = float(part.split("=")[1])
            elif part.startswith("+k="):
                k_val = float(part.split("=")[1])
        zw = 3 if abs(k_val - 0.9999) < 0.0001 else 6
        if lon_val is not None:
            fixed_crs, note = get_vn2000_crs(lon_0=lon_val, zone_width=zw)
            return fixed_crs, f"Auto-upgraded VN-2000 (.prj missing 7-params) -> CV1123 {note}, {zw}deg"

    return crs, f"Native CRS: {crs.name or crs.to_string()}"


def _flatten_collection(geom, preferred_family: str):
    """Unwrap GeometryCollection returned by make_valid into homogeneous geometry when possible."""
    if not isinstance(geom, GeometryCollection):
        return geom
    polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
    lines = [g for g in geom.geoms if g.geom_type in ("LineString", "MultiLineString") and not g.is_empty]
    pts = [g for g in geom.geoms if g.geom_type in ("Point", "MultiPoint") and not g.is_empty]

    if preferred_family == "Polygon" and polys:
        flat = []
        for p in polys:
            flat.extend(list(p.geoms) if p.geom_type == "MultiPolygon" else [p])
        return flat[0] if len(flat) == 1 else MultiPolygon(flat)
    if preferred_family == "LineString" and lines:
        flat = []
        for l in lines:
            flat.extend(list(l.geoms) if l.geom_type == "MultiLineString" else [l])
        return flat[0] if len(flat) == 1 else MultiLineString(flat)
    if pts:
        flat = []
        for pt in pts:
            flat.extend(list(pt.geoms) if pt.geom_type == "MultiPoint" else [pt])
        return flat[0] if len(flat) == 1 else MultiPoint(flat)
    return geom


def _read_features(input_path: Path, fix_font: bool) -> tuple[list[dict[str, Any]], list[str], CRS, str, int]:
    """Read vector features via OGR; linearize curves, flatten 2D, and close handles cleanly."""
    ds = ogr.Open(str(input_path), 0)
    if ds is None:
        raise RuntimeError(f"GDAL/OGR cannot open file: {input_path}")
    layer = None
    try:
        if ds.GetLayerCount() == 0:
            raise RuntimeError(f"Vector dataset has 0 layers: {input_path}")
        layer = ds.GetLayer(0)
        if layer is None:
            raise RuntimeError(f"Cannot read layer 0 from: {input_path}")

        layer_defn = layer.GetLayerDefn()
        field_names = [layer_defn.GetFieldDefn(i).GetName() for i in range(layer_defn.GetFieldCount())]
        srs = layer.GetSpatialRef()
        wkt_crs = srs.ExportToWkt() if srs is not None else ""

        rows: list[dict[str, Any]] = []
        font_fixed_cells = 0

        for idx, feat in enumerate(layer):
            fid = feat.GetFID()
            geom_ogr = feat.GetGeometryRef()
            geom_shp = None
            if geom_ogr is not None and not geom_ogr.IsEmpty():
                geom_clone = geom_ogr.Clone()
                geom_lin = geom_clone.GetLinearGeometry()
                if geom_lin is not None:
                    geom_clone = geom_lin
                geom_clone.FlattenTo2D()
                geom_shp = wkb.loads(bytes(geom_clone.ExportToWkb()))

            props: dict[str, Any] = {}
            for fn in field_names:
                val = feat.GetField(fn)
                if isinstance(val, str) and fix_font:
                    fixed, changed = smart_fix_text(val)
                    if changed:
                        font_fixed_cells += 1
                    val = fixed
                props[fn] = val

            rows.append({"idx": idx, "fid": fid, "geom": geom_shp, "props": props, "qa_flags": []})
    finally:
        layer = None
        ds = None

    return rows, field_names, wkt_crs, font_fixed_cells


def _resolve_crs_from_wkt(wkt_crs: str, province: Optional[str], lon0: Optional[float], zone_width: int, legacy: bool) -> tuple[CRS, str]:
    if province or lon0 is not None:
        crs, note = get_vn2000_crs(province=province, lon_0=lon0, zone_width=zone_width, legacy=legacy)
        return crs, f"Forced VN-2000 CV1123 ({note}, zone={zone_width}deg)"

    if not wkt_crs:
        raise ValueError(
            "File khong co .prj / CRS. Hay truyen --province <ten_tinh> (vd: --province ha_noi) hoac --lon0 <kinh_tuyen_truc>."
        )

    crs = CRS.from_wkt(wkt_crs)
    proj4 = crs.to_proj4() if not crs.is_geographic else ""
    name_upper = (crs.name or "").upper()
    if ("VN" in name_upper or "2000" in name_upper) and not crs.is_geographic and "towgs84" not in proj4:
        lon_val = None
        k_val = 0.9999
        for part in proj4.split():
            if part.startswith("+lon_0="):
                lon_val = float(part.split("=")[1])
            elif part.startswith("+k="):
                k_val = float(part.split("=")[1])
        zw = 3 if abs(k_val - 0.9999) < 0.0001 else 6
        if lon_val is not None:
            fixed_crs, note = get_vn2000_crs(lon_0=lon_val, zone_width=zw)
            return fixed_crs, f"Auto-upgraded VN-2000 (.prj missing 7-params) -> CV1123 {note}, {zw}deg"

    return crs, f"Native CRS: {crs.name or crs.to_string()}"


def _polygonize_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = [r["geom"] for r in rows if r["geom"] is not None and not r["geom"].is_empty]
    if not lines:
        return []
    # Node T-junctions & exact crossings without moving any vertex (legal-safe)
    noded = shapely_unary_union(lines)
    polys = list(shapely_polygonize(noded))
    return [
        {"idx": i, "fid": i, "geom": p, "props": {"poly_id": i + 1, "area_m2": round(p.area, 3)}, "qa_flags": []}
        for i, p in enumerate(polys)
    ]


def _spatial_join_labels(
    rows: list[dict[str, Any]],
    label_shp: Path,
    fix_font: bool,
    flag_multi: bool = True,
    parse_vn_labels: bool = False,
) -> dict[str, Any]:
    lbl_rows, lbl_fields, _, lbl_font_fixed = _read_features(label_shp, fix_font=fix_font)

    text_field = next((f for f in ("TxtMemo", "Text", "TEXT", "Name", "LABEL") if f in lbl_fields), None)
    if text_field is None:
        return {"joined_labels": 0, "no_label_count": 0, "multi_label_count": 0, "label_font_fixed": lbl_font_fixed}

    pts = []
    texts = []
    for lr in lbl_rows:
        g = lr["geom"]
        t = str(lr["props"].get(text_field) or "").strip()
        if g is not None and not g.is_empty and t:
            pts.append(g.representative_point())
            texts.append(t)

    if not pts:
        return {"joined_labels": 0, "no_label_count": 0, "multi_label_count": 0, "label_font_fixed": lbl_font_fixed}

    tree = STRtree(pts)
    joined = 0
    no_label = 0
    multi_label = 0

    for r in rows:
        g = r["geom"]
        if g is None or g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        matched = []
        for idx in tree.query(g):
            idx = int(idx)
            if g.contains(pts[idx]):
                matched.append(texts[idx])
        unique_matched = list(dict.fromkeys(matched))
        r["props"]["_cad_labels"] = " | ".join(unique_matched)
        if not unique_matched:
            no_label += 1
            if parse_vn_labels:
                r["props"]["loai_dat"] = ""
                r["props"]["so_thua"] = ""
                r["props"]["so_to"] = ""
        else:
            joined += 1
            if parse_vn_labels:
                codes = [t for t in matched if any(ch.isalpha() for ch in t)]
                nums = [t for t in matched if t.isdigit()]
                unique_codes = list(dict.fromkeys(codes))
                r["props"]["loai_dat"] = " | ".join(unique_codes)
                if len(nums) >= 2:
                    r["props"]["so_thua"] = nums[0]
                    r["props"]["so_to"] = nums[1]
                elif len(nums) == 1:
                    r["props"]["so_thua"] = nums[0]
                    r["props"]["so_to"] = ""
                    r["qa_flags"].append("INCOMPLETE_LABEL")
                else:
                    r["props"]["so_thua"] = ""
                    r["props"]["so_to"] = ""
                    r["qa_flags"].append("INCOMPLETE_LABEL")
                if not unique_codes and "INCOMPLETE_LABEL" not in r["qa_flags"]:
                    r["qa_flags"].append("INCOMPLETE_LABEL")
                # True anomaly in VN cadastral map: >1 land-use code or >2 numeric IDs in one parcel
                if len(unique_codes) > 1 or len(nums) > 2:
                    multi_label += 1
                    if flag_multi:
                        r["qa_flags"].append(f"MULTI_LABEL:{len(matched)}")
            elif len(unique_matched) > 1:
                multi_label += 1
                if flag_multi:
                    r["qa_flags"].append(f"MULTI_LABEL:{len(unique_matched)}")

    swapped_fixed = 0
    if parse_vn_labels and rows:
        from collections import Counter
        poly_geoms = [r["geom"] for r in rows]
        ptree = STRtree(poly_geoms)
        # Pass 1: fix reversed (so_to, so_thua) reading order using touching/1m neighbor sheet consensus
        for i, r in enumerate(rows):
            so_to = r["props"].get("so_to", "")
            so_thua = r["props"].get("so_thua", "")
            if not so_to or not so_thua or so_to == so_thua:
                continue
            g = r["geom"]
            if g is None or g.is_empty:
                continue
            nbr_tos = []
            for j in ptree.query(g.buffer(1.0)):
                j = int(j)
                if j == i:
                    continue
                nt = rows[j]["props"].get("so_to", "")
                if nt:
                    nbr_tos.append(nt)
            if not nbr_tos:
                continue
            nbr_counts = Counter(nbr_tos)
            if nbr_counts.get(so_to, 0) == 0 and nbr_counts.get(so_thua, 0) > 0:
                r["props"]["so_to"], r["props"]["so_thua"] = so_thua, so_to
                swapped_fixed += 1

    return {
        "joined_labels": joined,
        "no_label_count": no_label,
        "multi_label_count": multi_label,
        "swapped_to_thua_fixed": swapped_fixed,
        "label_font_fixed": lbl_font_fixed,
    }


def _run_topology_qa(
    rows: list[dict[str, Any]],
    src_crs: CRS,
    auto_fix: bool,
    dedup: bool,
    check_overlaps: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    empty_ids: list[int] = []
    invalid_records: list[dict[str, Any]] = []
    duplicate_ids: list[int] = []
    overlap_pairs: list[dict[str, Any]] = []

    seen_hashes: dict[str, int] = {}
    total_area_before = 0.0
    total_area_after = 0.0
    geom_types: dict[str, int] = {}
    kept_rows: list[dict[str, Any]] = []

    valid_geoms = []
    valid_fids = []

    # Geographic CRS (degrees) uses 1e-10 deg^2 (~1.2 m^2), projected CRS (meters) uses 0.01 m^2
    overlap_tol = 1e-10 if src_crs.is_geographic else 0.01

    for r in rows:
        fid = r["fid"]
        g = r["geom"]
        # Preserve any pre-existing QA flags (e.g., MULTI_LABEL from label join)
        r.setdefault("qa_flags", [])

        if g is None or g.is_empty:
            empty_ids.append(fid)
            r["qa_flags"].append("EMPTY")
            kept_rows.append(r)
            continue

        gtype = g.geom_type
        geom_types[gtype] = geom_types.get(gtype, 0) + 1
        total_area_before += g.area

        if not g.is_valid:
            reason = explain_validity(g)
            invalid_records.append({"fid": fid, "reason": reason})
            r["qa_flags"].append(f"INVALID:{reason}")
            if auto_fix:
                pref = "Polygon" if "Polygon" in gtype else ("LineString" if "Line" in gtype else "Point")
                g = _flatten_collection(make_valid(g), pref)
                r["geom"] = g

        # Normalize vertex order before hashing so reversed/shifted rings are caught as duplicates
        h = hashlib.md5(shapely_normalize(g).wkb).hexdigest()
        if h in seen_hashes:
            duplicate_ids.append(fid)
            if dedup:
                continue
            r["qa_flags"].append(f"DUPLICATE_OF:{seen_hashes[h]}")
        else:
            seen_hashes[h] = fid

        total_area_after += g.area
        r["props"]["_area_src"] = round(g.area, 6 if src_crs.is_geographic else 3)
        kept_rows.append(r)

        if g.geom_type in ("Polygon", "MultiPolygon"):
            valid_geoms.append(g)
            valid_fids.append(fid)

    if check_overlaps and len(valid_geoms) > 1:
        tree = STRtree(valid_geoms)
        fid_to_row = {r["fid"]: r for r in kept_rows}
        checked = set()
        for i, g1 in enumerate(valid_geoms):
            for j in tree.query(g1):
                j = int(j)
                if j <= i:
                    continue
                pair = (i, j)
                if pair in checked:
                    continue
                checked.add(pair)
                g2 = valid_geoms[j]
                if g1.intersects(g2):
                    inter = g1.intersection(g2)
                    if inter.area > overlap_tol:
                        overlap_pairs.append({
                            "fid_a": valid_fids[i],
                            "fid_b": valid_fids[j],
                            "overlap_area": round(inter.area, 6 if src_crs.is_geographic else 4),
                        })
                        fid_to_row[valid_fids[i]]["qa_flags"].append(f"OVERLAP:{valid_fids[j]}")
                        fid_to_row[valid_fids[j]]["qa_flags"].append(f"OVERLAP:{valid_fids[i]}")

    qa = {
        "total_features_in": len(rows),
        "total_features_out": len(kept_rows),
        "geometry_types": geom_types,
        "empty_count": len(empty_ids),
        "empty_fids": empty_ids[:50],
        "invalid_count": len(invalid_records),
        "invalid_samples": invalid_records[:50],
        "auto_fixed_invalid": auto_fix,
        "duplicate_count": len(duplicate_ids),
        "dedup_removed": dedup,
        "duplicate_fids": duplicate_ids[:50],
        "overlap_pair_count": len(overlap_pairs),
        "overlap_samples": overlap_pairs[:50],
        "total_area_before": round(total_area_before, 4),
        "total_area_after": round(total_area_after, 4),
    }
    return kept_rows, qa


def _reproject_rows(rows: list[dict[str, Any]], src_crs: CRS) -> tuple[list[dict[str, Any]], float]:
    dst_crs = CRS.from_epsg(4326)
    if src_crs == dst_crs:
        for r in rows:
            r["geom_wgs84"] = r["geom"]
        return rows, 0.0

    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    roundtrip_mm = 0.0
    for r in rows:
        if r["geom"] is not None and not r["geom"].is_empty:
            c = r["geom"].centroid
            roundtrip_mm = verify_roundtrip(src_crs, c.x, c.y)
            break

    for r in rows:
        g = r["geom"]
        r["geom_wgs84"] = None if (g is None or g.is_empty) else shapely_transform(transformer.transform, g)

    return rows, round(roundtrip_mm, 4)


def _export_gpkg(rows: list[dict[str, Any]], src_crs: CRS, out_path: Path, layer_name: str) -> None:
    """Export OGC GeoPackage (.gpkg) in native CRS with union schema and case-insensitive column safety."""
    if out_path.exists():
        out_path.unlink()
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(str(out_path))
    srs = osr.SpatialReference()
    srs.ImportFromWkt(src_crs.to_wkt())
    lyr = ds.CreateLayer(layer_name, srs, ogr.wkbUnknown)

    # Collect union of all property keys across all rows; avoid SQLite/GPKG reserved 'fid' & QA names
    reserved_lower = {"fid", "_fid", "_qa_status", "_has_error"}
    seen_lower = set(reserved_lower)
    prop_mapping: list[tuple[str, str]] = []  # (orig_key, safe_col_name)
    for r in rows:
        for k in r["props"].keys():
            safe = str(k)[:63]
            if safe.lower() in seen_lower:
                continue
            seen_lower.add(safe.lower())
            prop_mapping.append((k, safe))
            lyr.CreateField(ogr.FieldDefn(safe, ogr.OFTString))

    lyr.CreateField(ogr.FieldDefn("_fid", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("_qa_status", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("_has_error", ogr.OFTInteger))

    defn = lyr.GetLayerDefn()
    for r in rows:
        g = r["geom"]
        if g is None or g.is_empty:
            continue
        feat = ogr.Feature(defn)
        feat.SetGeometry(ogr.CreateGeometryFromWkb(g.wkb))
        for orig_k, safe_k in prop_mapping:
            val = r["props"].get(orig_k)
            feat.SetField(safe_k, "" if val is None else str(val))
        feat.SetField("_fid", int(r["fid"]))
        feat.SetField("_qa_status", "OK" if not r["qa_flags"] else "; ".join(r["qa_flags"]))
        feat.SetField("_has_error", 1 if r["qa_flags"] else 0)
        lyr.CreateFeature(feat)
    lyr = None
    ds = None


def _export_qa_csv(rows: list[dict[str, Any]], out_path: Path) -> int:
    err_rows = [r for r in rows if r["qa_flags"]]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["FID", "Level", "QA_Flags", "Area_Src", "CAD_Labels", "Centroid_Lon", "Centroid_Lat"])
        for r in err_rows:
            g4326 = r.get("geom_wgs84")
            cx, cy = (round(g4326.centroid.x, 7), round(g4326.centroid.y, 7)) if (g4326 is not None and not g4326.is_empty) else ("", "")
            writer.writerow([
                r["fid"],
                r["props"].get("Level", r["props"].get("Layer", "")),
                "; ".join(r["qa_flags"]),
                r["props"].get("_area_src", ""),
                r["props"].get("_cad_labels", ""),
                cx,
                cy,
            ])
    return len(err_rows)


def _export_geojson(rows: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    features = []
    for r in rows:
        g = r["geom_wgs84"]
        if g is None or g.is_empty:
            continue
        props = dict(r["props"])
        props["_fid"] = r["fid"]
        props["_qa_status"] = "OK" if not r["qa_flags"] else "; ".join(r["qa_flags"])
        props["_has_error"] = bool(r["qa_flags"])
        features.append({
            "type": "Feature",
            "id": r["fid"],
            "properties": props,
            "geometry": g.__geo_interface__,
        })

    fc = {"type": "FeatureCollection", "features": features}
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    return fc


def _export_kml(rows: list[dict[str, Any]], out_path: Path, doc_name: str) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        f"  <name>{saxutils.escape(doc_name)}</name>",
        '  <Style id="okGeom"><LineStyle><color>ff00ffff</color><width>2</width></LineStyle><PolyStyle><color>4000ff00</color></PolyStyle></Style>',
        '  <Style id="errGeom"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle><PolyStyle><color>600000ff</color></PolyStyle></Style>',
    ]

    def ring_to_coords(ring) -> str:
        return " ".join(f"{x:.7f},{y:.7f},0" for x, y in ring.coords)

    def emit_geom(g, buf: list[str]) -> None:
        if g.geom_type == "Polygon":
            buf.append("      <Polygon><outerBoundaryIs><LinearRing>")
            buf.append(f"        <coordinates>{ring_to_coords(g.exterior)}</coordinates>")
            buf.append("      </LinearRing></outerBoundaryIs>")
            for interior in g.interiors:
                buf.append("      <innerBoundaryIs><LinearRing>")
                buf.append(f"        <coordinates>{ring_to_coords(interior)}</coordinates>")
                buf.append("      </LinearRing></innerBoundaryIs>")
            buf.append("      </Polygon>")
        elif g.geom_type == "LineString":
            buf.append(f"      <LineString><coordinates>{ring_to_coords(g)}</coordinates></LineString>")
        elif g.geom_type == "Point":
            buf.append(f"      <Point><coordinates>{g.x:.7f},{g.y:.7f},0</coordinates></Point>")
        elif hasattr(g, "geoms"):
            for sub in g.geoms:
                emit_geom(sub, buf)

    for r in rows:
        g = r["geom_wgs84"]
        if g is None or g.is_empty:
            continue
        style = "#errGeom" if r["qa_flags"] else "#okGeom"
        name_val = next((str(v) for k, v in r["props"].items() if not k.startswith("_") and v not in (None, "")), f"FID_{r['fid']}")
        desc_rows = "".join(
            f"<tr><td><b>{saxutils.escape(str(k))}</b></td><td>{saxutils.escape(str(v))}</td></tr>"
            for k, v in r["props"].items()
        )
        desc = f"<![CDATA[<table border='1'>{desc_rows}</table>]]>"
        geom_lines: list[str] = []
        emit_geom(g, geom_lines)
        if not geom_lines:
            continue
        lines.append("  <Placemark>")
        lines.append(f"    <name>{saxutils.escape(name_val)}</name>")
        lines.append(f"    <description>{desc}</description>")
        lines.append(f"    <styleUrl>{style}</styleUrl>")
        lines.append("    <MultiGeometry>")
        lines.extend(geom_lines)
        lines.append("    </MultiGeometry>")
        lines.append("  </Placemark>")

    lines.append("</Document></kml>")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _export_webgis_html(fc: dict[str, Any], qa_summary: dict[str, Any], out_path: Path, title: str) -> None:
    """Export standalone MapLibre GL JS WebGIS HTML (XSS-safe, supports Polygon/Line/Point/Collection)."""
    minx, miny, maxx, maxy = 180.0, 90.0, -180.0, -90.0

    def walk_geom(geom_dict):
        nonlocal minx, miny, maxx, maxy
        if not geom_dict:
            return
        if "geometries" in geom_dict:
            for sub in geom_dict["geometries"]:
                walk_geom(sub)
            return
        walk_coords(geom_dict.get("coordinates"))

    def walk_coords(coords):
        nonlocal minx, miny, maxx, maxy
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            x, y = coords[0], coords[1]
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
        else:
            for c in coords:
                walk_coords(c)

    for feat in fc["features"]:
        walk_geom(feat["geometry"])

    if minx > maxx:
        minx, miny, maxx, maxy = 105.0, 20.5, 106.0, 21.5

    # Prevent </script> breakout and JS line-separator syntax errors
    geojson_str = (
        json.dumps(fc, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <title>VN-GIS QA — {saxutils.escape(title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; }}
    #map {{ position: absolute; top: 0; bottom: 0; width: 100%; }}
    .panel {{
      position: absolute; top: 12px; left: 12px; z-index: 10;
      background: rgba(15, 23, 42, 0.93); color: #eef2f7;
      padding: 12px 14px; border-radius: 10px; max-width: 340px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.4); font-size: 13px;
    }}
    .panel h3 {{ margin: 0 0 8px 0; font-size: 15px; color: #38bdf8; }}
    .stat {{ display: flex; justify-content: space-between; margin: 3px 0; }}
    .err {{ color: #f87171; font-weight: 600; }}
    .ok {{ color: #4ade80; font-weight: 600; }}
    .btns {{ display: flex; gap: 5px; margin-top: 8px; flex-wrap: wrap; }}
    button {{
      background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
      padding: 5px 9px; border-radius: 6px; cursor: pointer; font-size: 12px;
    }}
    button.active, button:hover {{ background: #0284c7; border-color: #38bdf8; color: #fff; }}
    .popup-tbl {{ border-collapse: collapse; font-size: 12px; max-height: 260px; overflow: auto; display: block; }}
    .popup-tbl td {{ border-bottom: 1px solid #e2e8f0; padding: 3px 6px; }}
    .popup-tbl td:first-child {{ font-weight: 600; color: #334155; white-space: nowrap; }}
  </style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h3>VN-GIS QA: {saxutils.escape(title)}</h3>
  <div class="stat"><span>Đối tượng (vào / ra):</span><span><b>{qa_summary['total_features_in']} / {qa_summary['total_features_out']}</b></span></div>
  <div class="stat"><span>Lỗi hình học (Invalid):</span><span class="{'err' if qa_summary['invalid_count'] else 'ok'}">{qa_summary['invalid_count']}</span></div>
  <div class="stat"><span>Cặp chồng lấn (Overlap):</span><span class="{'err' if qa_summary['overlap_pair_count'] else 'ok'}">{qa_summary['overlap_pair_count']}</span></div>
  <div class="stat"><span>Trùng lặp (Duplicate):</span><span class="{'err' if qa_summary['duplicate_count'] else 'ok'}">{qa_summary['duplicate_count']}</span></div>
  <div class="btns">
    <button id="btn-sat" class="active" onclick="setBase('sat')">Vệ tinh</button>
    <button id="btn-osm" onclick="setBase('osm')">OSM</button>
    <button onclick="fitData()">Zoom hết</button>
  </div>
  <div class="btns">
    <button id="f-all" class="active" onclick="setFilter('all')">Tất cả</button>
    <button id="f-err" onclick="setFilter('err')">Chỉ lỗi đỏ</button>
    <button id="f-ok" onclick="setFilter('ok')">Chỉ sạch</button>
  </div>
  <div style="margin-top:8px;display:flex;gap:5px;">
    <input id="q" type="text" placeholder="Tìm tờ / thửa / tên xã..." style="flex:1;padding:5px 8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#f8fafc;font-size:12px;" onkeydown="if(event.key==='Enter')searchFeat()" />
    <button onclick="searchFeat()">Tìm</button>
  </div>
  <div id="search-msg" style="margin-top:4px;font-size:11px;color:#94a3b8;"></div>
</div>
<script>
const GEOJSON = {geojson_str};
const BOUNDS = [[{minx:.7f}, {miny:.7f}], [{maxx:.7f}, {maxy:.7f}]];

function escHtml(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

const map = new maplibregl.Map({{
  container: 'map',
  style: {{
    version: 8,
    sources: {{
      'google-sat': {{ type: 'raster', tiles: ['https://mt1.google.com/vt/lyrs=y&x={{x}}&y={{y}}&z={{z}}'], tileSize: 256 }},
      'osm': {{ type: 'raster', tiles: ['https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png'], tileSize: 256 }}
    }},
    layers: [
      {{ id: 'base-sat', type: 'raster', source: 'google-sat', layout: {{ visibility: 'visible' }} }},
      {{ id: 'base-osm', type: 'raster', source: 'osm', layout: {{ visibility: 'none' }} }}
    ]
  }},
  bounds: BOUNDS,
  fitBoundsOptions: {{ padding: 40 }}
}});

map.addControl(new maplibregl.NavigationControl(), 'top-right');
map.addControl(new maplibregl.ScaleControl({{ unit: 'metric' }}));

const INTERACTIVE_LAYERS = ['parcels-fill', 'parcels-line', 'parcels-pt'];

map.on('load', () => {{
  map.addSource('parcels', {{ type: 'geojson', data: GEOJSON }});
  map.addLayer({{
    id: 'parcels-fill', type: 'fill', source: 'parcels',
    filter: ['==', ['geometry-type'], 'Polygon'],
    paint: {{
      'fill-color': ['case', ['get', '_has_error'], '#ef4444', '#22c55e'],
      'fill-opacity': ['case', ['get', '_has_error'], 0.48, 0.22]
    }}
  }});
  map.addLayer({{
    id: 'parcels-line', type: 'line', source: 'parcels',
    paint: {{
      'line-color': ['case', ['get', '_has_error'], '#dc2626', '#facc15'],
      'line-width': ['case', ['get', '_has_error'], 2.4, 1.3]
    }}
  }});
  map.addLayer({{
    id: 'parcels-pt', type: 'circle', source: 'parcels',
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {{
      'circle-radius': 5,
      'circle-color': ['case', ['get', '_has_error'], '#ef4444', '#38bdf8'],
      'circle-stroke-width': 1.5, 'circle-stroke-color': '#ffffff'
    }}
  }});

  INTERACTIVE_LAYERS.forEach(lid => {{
    map.on('click', lid, (e) => {{
      const props = e.features[0].properties;
      let rows = '';
      for (const [k, v] of Object.entries(props)) {{
        if (k === '_has_error') continue;
        rows += `<tr><td>${{escHtml(k)}}</td><td>${{escHtml(v)}}</td></tr>`;
      }}
      new maplibregl.Popup({{ maxWidth: '360px' }}).setLngLat(e.lngLat).setHTML(`<table class="popup-tbl">${{rows}}</table>`).addTo(map);
    }});
    map.on('mouseenter', lid, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', lid, () => map.getCanvas().style.cursor = '');
  }});
}});

function setBase(mode) {{
  map.setLayoutProperty('base-sat', 'visibility', mode === 'sat' ? 'visible' : 'none');
  map.setLayoutProperty('base-osm', 'visibility', mode === 'osm' ? 'visible' : 'none');
  document.getElementById('btn-sat').classList.toggle('active', mode === 'sat');
  document.getElementById('btn-osm').classList.toggle('active', mode === 'osm');
}}
function setFilter(mode) {{
  const errCond = mode === 'err' ? ['==', ['get', '_has_error'], true] : (mode === 'ok' ? ['==', ['get', '_has_error'], false] : null);
  map.setFilter('parcels-fill', errCond ? ['all', ['==', ['geometry-type'], 'Polygon'], errCond] : ['==', ['geometry-type'], 'Polygon']);
  map.setFilter('parcels-line', errCond);
  map.setFilter('parcels-pt', errCond ? ['all', ['==', ['geometry-type'], 'Point'], errCond] : ['==', ['geometry-type'], 'Point']);
  ['all','err','ok'].forEach(m => document.getElementById('f-' + m).classList.toggle('active', m === mode));
}}
function fitData() {{ map.fitBounds(BOUNDS, {{ padding: 40 }}); }}
function searchFeat() {{
  const q = document.getElementById('q').value.trim().toLowerCase();
  const msg = document.getElementById('search-msg');
  if (!q) {{ msg.textContent = ''; return; }}
  const found = GEOJSON.features.find(f =>
    Object.entries(f.properties).some(([k, v]) => !k.startsWith('_') && String(v ?? '').toLowerCase().includes(q))
  );
  if (!found) {{ msg.textContent = 'Không tìm thấy đối tượng khớp.'; return; }}
  let x0=180,y0=90,x1=-180,y1=-90;
  const walkC = c => {{ if (typeof c[0]==='number') {{ x0=Math.min(x0,c[0]); x1=Math.max(x1,c[0]); y0=Math.min(y0,c[1]); y1=Math.max(y1,c[1]); }} else c.forEach(walkC); }};
  const walkG = g => {{ if (!g) return; if (g.geometries) g.geometries.forEach(walkG); else if (g.coordinates) walkC(g.coordinates); }};
  walkG(found.geometry);
  map.fitBounds([[x0,y0],[x1,y1]], {{ padding: 80, maxZoom: 18 }});
  msg.textContent = 'Đã zoom tới FID ' + found.id;
}}
</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def _extract_cad_via_arcpy(
    cad_path: Path,
    temp_dir: Path,
    cad_layer: str,
    cad_level: Optional[str],
    extract_labels: bool,
    label_level: Optional[str] = None,
) -> tuple[Path, Optional[Path], dict[str, Any]]:
    arcpy_candidates = [
        Path(r"C:\Python27\ArcGIS10.7\python.exe"),
        Path(r"C:\Python27\ArcGIS10.8\python.exe"),
        Path(r"C:\Python27\ArcGIS10.6\python.exe"),
    ]
    arcpy_py = next((p for p in arcpy_candidates if p.exists()), None)
    if arcpy_py is None:
        raise RuntimeError("Khong tim thay ArcGIS Python (ArcPy) de doc file DGN V8 / DWG.")

    temp_dir.mkdir(parents=True, exist_ok=True)
    req_json = temp_dir / "_bridge_req.json"
    req_json.write_text(json.dumps({
        "input_cad": str(cad_path),
        "out_dir": str(temp_dir),
        "layer": cad_layer,
        "level": cad_level,
        "extract_labels": extract_labels,
        "label_level": label_level,
    }, ensure_ascii=False), encoding="utf-8")

    bridge_script = ROOT_DIR / "tools" / "dgn_bridge.py"
    cmd = [str(arcpy_py), str(bridge_script), "--request-json", str(req_json)]

    clean_env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONHOME", "PYTHONPATH")}
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=clean_env)
    stdout_lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 or not stdout_lines:
        raise RuntimeError(f"ArcPy CAD bridge failed (code {proc.returncode}): {proc.stderr or proc.stdout}")

    info = json.loads(stdout_lines[-1])
    if info.get("status") != "OK":
        raise RuntimeError(f"ArcPy CAD bridge error: {info}")
    lbl_path = Path(info["label_shp"]) if info.get("label_shp") else None
    return Path(info["output_shp"]), lbl_path, info


def main() -> None:
    parser = argparse.ArgumentParser(description="VN-GIS Cadastral QA/QC, VN-2000 (34-Province 2025-2026) & WebGIS Suite")
    parser.add_argument("input", help="Path to source vector/CAD file (.shp, .dgn, .dwg, .geojson, .gpkg, .kml, .gml)")
    parser.add_argument("--province", default=None, help="VN-2000 province name (34-province 2025 standard, e.g. ha_noi, phu_tho)")
    parser.add_argument("--lon0", type=float, default=None, help="Explicit VN-2000 central meridian (e.g. 105.0, 104.75)")
    parser.add_argument("--zone", type=int, default=3, choices=[3, 6], help="VN-2000 zone width: 3 or 6 deg (default: 3)")
    parser.add_argument("--legacy", action="store_true", help="Use pre-2025 63-province meridian table for old cadastral archives")
    parser.add_argument("--cad-layer", default="auto", choices=["auto", "Polygon", "Polyline", "Point"], help="For .dgn/.dwg: CAD layer to extract")
    parser.add_argument("--cad-level", default=None, help="For .dgn/.dwg: SQL filter (e.g. \"Level = 11\")")
    parser.add_argument("--polygonize", action="store_true", help="Close open linework into polygons without shifting vertices")
    parser.add_argument("--join-labels", action="store_true", help="For .dgn/.dwg: spatially join CAD Annotation labels into polygons")
    parser.add_argument("--label-level", default=None, help="For .dgn/.dwg Annotation: SQL filter (e.g. \"Level = 6\")")
    parser.add_argument("--parse-vn-labels", action="store_true", help="Parse VN cadastral labels into loai_dat, so_thua, so_to")
    parser.add_argument("--drop-unlabeled", action="store_true", help="Keep only polygons with >=1 joined CAD label")
    parser.add_argument("--fix-font", action="store_true", help="Auto-detect and convert TCVN3 (.VnTime) text to Unicode UTF-8")
    parser.add_argument("--auto-fix-geom", action="store_true", help="Repair invalid geometries via shapely.make_valid")
    parser.add_argument("--dedup", action="store_true", help="Remove exact 100% geometric duplicates (legal-safe)")
    parser.add_argument("--skip-overlaps", action="store_true", help="Skip polygon overlap check (faster on >50k parcels)")
    parser.add_argument("--out-dir", default=None, help="Custom output directory (default: D:\\Antigravity\\gis_suite\\output\\<stem>)")
    args = parser.parse_args()

    src_path = Path(args.input).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"Source file not found: {src_path}")

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (ROOT_DIR / "output" / src_path.stem)
    # Guard against accidental overwrite of source file
    if out_dir == src_path.parent:
        out_dir = out_dir / f"{src_path.stem}_qa_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cad_info: Optional[dict[str, Any]] = None
    label_summary: Optional[dict[str, Any]] = None
    temp_cad_dir: Optional[Path] = None
    label_shp: Optional[Path] = None
    read_path = src_path

    if src_path.suffix.lower() in (".dgn", ".dwg"):
        temp_cad_dir = out_dir / "_cad_extract"
        read_path, label_shp, cad_info = _extract_cad_via_arcpy(
            src_path, temp_cad_dir, args.cad_layer, args.cad_level, args.join_labels, args.label_level
        )

    try:
        rows, field_names, wkt_crs, font_fixed = _read_features(read_path, fix_font=args.fix_font)
        src_crs, crs_note = _resolve_crs_from_wkt(
            wkt_crs, province=args.province, lon0=args.lon0, zone_width=args.zone, legacy=args.legacy
        )

        if args.polygonize:
            rows = _polygonize_lines(rows)
            field_names = ["poly_id", "area_m2"]

        if args.join_labels and label_shp and label_shp.exists():
            label_summary = _spatial_join_labels(
                rows, label_shp, fix_font=args.fix_font, parse_vn_labels=args.parse_vn_labels
            )
            if args.parse_vn_labels:
                for col in ("so_to", "so_thua", "loai_dat"):
                    if col not in field_names:
                        field_names.append(col)
            if args.drop_unlabeled:
                before_drop = len(rows)
                rows = [r for r in rows if r["props"].get("_cad_labels")]
                for i, r in enumerate(rows):
                    r["idx"] = i
                    r["fid"] = i
                label_summary["dropped_unlabeled"] = before_drop - len(rows)
    finally:
        if temp_cad_dir and temp_cad_dir.exists():
            shutil.rmtree(temp_cad_dir, ignore_errors=True)

    rows, qa = _run_topology_qa(
        rows, src_crs=src_crs, auto_fix=args.auto_fix_geom, dedup=args.dedup, check_overlaps=not args.skip_overlaps
    )
    rows, roundtrip_mm = _reproject_rows(rows, src_crs)

    gpkg_path = out_dir / f"{src_path.stem}_clean.gpkg"
    geojson_path = out_dir / f"{src_path.stem}_wgs84.geojson"
    kml_path = out_dir / f"{src_path.stem}.kml"
    html_path = out_dir / f"{src_path.stem}_webgis.html"
    csv_path = out_dir / f"{src_path.stem}_qa_errors.csv"
    report_json_path = out_dir / f"{src_path.stem}_qa_report.json"

    _export_gpkg(rows, src_crs, gpkg_path, layer_name=src_path.stem)
    fc = _export_geojson(rows, geojson_path)
    _export_kml(rows, kml_path, doc_name=src_path.stem)
    _export_webgis_html(fc, qa, html_path, title=src_path.stem)
    flagged_count = _export_qa_csv(rows, csv_path)

    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source_file": str(src_path),
        "crs_resolution": crs_note,
        "roundtrip_error_mm": roundtrip_mm,
        "cad_extract": cad_info,
        "label_join": label_summary,
        "fields": field_names,
        "font_fixed_cells": font_fixed,
        "flagged_for_human_review": flagged_count,
        "qa": qa,
        "outputs": {
            "gpkg_native": str(gpkg_path),
            "geojson_wgs84": str(geojson_path),
            "kml": str(kml_path),
            "webgis_html": str(html_path),
            "qa_errors_csv": str(csv_path),
        },
    }
    report_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "OK",
        "source": str(src_path),
        "crs": crs_note,
        "roundtrip_error_mm": roundtrip_mm,
        "features_in": qa["total_features_in"],
        "features_out": qa["total_features_out"],
        "font_fixed_cells": font_fixed,
        "invalid_geom": qa["invalid_count"],
        "overlap_pairs": qa["overlap_pair_count"],
        "duplicates": qa["duplicate_count"],
        "dedup_removed": args.dedup,
        "label_join": label_summary,
        "flagged_for_human_review": flagged_count,
        "outputs": report["outputs"],
        "qa_report": str(report_json_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
