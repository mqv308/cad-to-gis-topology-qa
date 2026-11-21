"""VN-2000 (CV 1123/BTNMT 7-parameter Helmert) <-> WGS84 CRS engine.

Defaults to the 34 merged provinces/cities (2025-2026 administrative standard).
Pass legacy=True to resolve pre-2025 63-province central meridians for historical cadastral maps.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

from pyproj import CRS, Transformer

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "vn2000_vietnam.json"


def _load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_province(name: str) -> str:
    text = unicodedata.normalize("NFD", name.strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    aliases = {
        "hanoi": "ha_noi",
        "hn": "ha_noi",
        "hcm": "tp_hcm",
        "ho_chi_minh": "tp_hcm",
        "tphcm": "tp_hcm",
        "tp_ho_chi_minh": "tp_hcm",
        "phutho": "phu_tho",
        "vinhphuc": "vinh_phuc",
        "bacninh": "bac_ninh",
        "bacgiang": "bac_giang",
        "quangninh": "quang_ninh",
        "haiphong": "hai_phong",
        "danang": "da_nang",
        "thua_thien_hue": "hue",
        "vungtau": "ba_ria_vung_tau",
    }
    return aliases.get(text, text)


def resolve_meridian(province: str, legacy: bool = False) -> tuple[float, str]:
    """Return (lon_0, resolution_note) for 34-province 2025 standard or legacy 63-province archive."""
    cfg = _load_config()
    key = _normalize_province(province)

    if legacy:
        table = cfg["legacy_63_pre2025"]
        if key == "hue":
            key = "thua_thien_hue"
        if key not in table:
            raise KeyError(f"Unknown legacy province '{province}' (normalized: '{key}').")
        return float(table[key]), f"Legacy pre-2025 ({key}, lon_0={table[key]})"

    table34 = cfg["provinces_34_2025"]
    merged_map = cfg["merged_mapping_2025"]
    if key in table34:
        return float(table34[key]), f"34-province 2025 ({key}, lon_0={table34[key]})"
    if key in merged_map:
        new_prov = merged_map[key]
        return float(table34[new_prov]), f"34-province 2025 ({key} -> merged into {new_prov}, lon_0={table34[new_prov]})"

    raise KeyError(
        f"Unknown province '{province}' (normalized: '{key}'). "
        f"Check config/vn2000_vietnam.json or pass --lon0 explicitly."
    )


def build_vn2000_proj4(
    province: Optional[str] = None,
    lon_0: Optional[float] = None,
    zone_width: int = 3,
    legacy: bool = False,
) -> tuple[str, str]:
    """Build exact PROJ4 string for VN-2000 using CV 1123/BTNMT 7-parameter transform."""
    cfg = _load_config()
    meta = cfg["meta"]
    note = ""

    if lon_0 is None:
        if not province:
            raise ValueError("Must provide either province name or central meridian (lon_0).")
        lon_0, note = resolve_meridian(province, legacy=legacy)
    else:
        note = f"Explicit lon_0={lon_0}"

    k_0 = meta["k0_3deg"] if zone_width == 3 else meta["k0_6deg"]
    proj4 = (
        f"+proj=tmerc +lat_0=0 +lon_0={lon_0} +k={k_0} "
        f"+x_0={meta['false_easting']} +y_0={meta['false_northing']} "
        f"+ellps=WGS84 +towgs84={meta['towgs84_7params']} +units=m +no_defs"
    )
    return proj4, note


def get_vn2000_crs(
    province: Optional[str] = None,
    lon_0: Optional[float] = None,
    zone_width: int = 3,
    legacy: bool = False,
) -> tuple[CRS, str]:
    proj4, note = build_vn2000_proj4(province=province, lon_0=lon_0, zone_width=zone_width, legacy=legacy)
    return CRS.from_proj4(proj4), note


def verify_roundtrip(crs_vn2000: CRS, sample_x: float = 550000.0, sample_y: float = 2320000.0) -> float:
    """Return round-trip error in millimeters (VN-2000 -> WGS84 -> VN-2000)."""
    crs_wgs84 = CRS.from_epsg(4326)
    to_wgs = Transformer.from_crs(crs_vn2000, crs_wgs84, always_xy=True)
    to_vn = Transformer.from_crs(crs_wgs84, crs_vn2000, always_xy=True)
    lon, lat = to_wgs.transform(sample_x, sample_y)
    rx, ry = to_vn.transform(lon, lat)
    err_m = ((rx - sample_x) ** 2 + (ry - sample_y) ** 2) ** 0.5
    return err_m * 1000.0
