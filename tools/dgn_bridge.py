# -*- coding: utf-8 -*-
"""ArcPy bridge (Python 2.7 / ArcGIS 10.7) to extract DGN (V7/V8) and DWG layers to Shapefile.

Supports Unicode Vietnamese paths cleanly via JSON request file (--request-json) or Windows
wide-char argv, optional SQL Level filtering, and simultaneous Annotation extraction.
Never modifies source files.
"""

from __future__ import print_function
import argparse
import codecs
import json
import os
import sys

import arcpy

arcpy.env.overwriteOutput = True


def _load_params():
    parser = argparse.ArgumentParser(description="Extract CAD (.dgn/.dwg) layers to Shapefile via ArcPy")
    parser.add_argument("--request-json", default=None, help="UTF-8 JSON file with unicode paths & options")
    parser.add_argument("input_cad", nargs="?", default=None, help="Path to .dgn or .dwg file")
    parser.add_argument("out_dir", nargs="?", default=None, help="Output directory")
    parser.add_argument("--layer", default="auto", choices=["auto", "Polygon", "Polyline", "Point"])
    parser.add_argument("--level", default=None)
    parser.add_argument("--extract-labels", action="store_true")
    parser.add_argument("--label-level", default=None)
    args = parser.parse_args()

    if args.request_json:
        with codecs.open(args.request_json, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "input_cad": args.input_cad,
        "out_dir": args.out_dir,
        "layer": args.layer,
        "level": args.level,
        "extract_labels": args.extract_labels,
        "label_level": args.label_level,
    }


def main():
    params = _load_params()
    cad_path = os.path.abspath(params["input_cad"])
    out_dir = os.path.abspath(params["out_dir"])
    req_layer = params.get("layer", "auto")
    req_level = params.get("level")
    extract_labels = bool(params.get("extract_labels", False))
    req_label_level = params.get("label_level")

    if not os.path.exists(cad_path):
        print(json.dumps({"status": "ERROR", "error": u"File not found: " + cad_path}, ensure_ascii=True))
        sys.exit(1)

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    arcpy.env.workspace = cad_path
    fcs = arcpy.ListFeatureClasses() or []
    counts = {}
    for fc in [u"Polygon", u"Polyline", u"Point", u"Annotation"]:
        if fc in fcs:
            fc_path = os.path.join(cad_path, fc)
            try:
                counts[fc] = int(arcpy.GetCount_management(fc_path).getOutput(0))
            except Exception:
                counts[fc] = 0
        else:
            counts[fc] = 0

    chosen = req_layer
    if chosen == "auto":
        if counts.get(u"Polygon", 0) > 0:
            chosen = u"Polygon"
        elif counts.get(u"Polyline", 0) > 0:
            chosen = u"Polyline"
        elif counts.get(u"Point", 0) > 0:
            chosen = u"Point"
        else:
            print(json.dumps({"status": "ERROR", "error": "No non-empty geometry layers in CAD file", "counts": counts}, ensure_ascii=True))
            sys.exit(1)

    src_fc = os.path.join(cad_path, chosen)
    out_name = u"cad_{0}.shp".format(chosen.lower())
    out_shp = os.path.join(out_dir, out_name)

    if req_level:
        arcpy.FeatureClassToFeatureClass_conversion(src_fc, out_dir, out_name, req_level)
    else:
        arcpy.FeatureClassToFeatureClass_conversion(src_fc, out_dir, out_name)

    extracted_count = int(arcpy.GetCount_management(out_shp).getOutput(0))

    label_shp = None
    label_count = 0
    if extract_labels and counts.get(u"Annotation", 0) > 0:
        ann_fc = os.path.join(cad_path, u"Annotation")
        ann_name = u"cad_annotation.shp"
        if req_label_level:
            arcpy.FeatureClassToFeatureClass_conversion(ann_fc, out_dir, ann_name, req_label_level)
        else:
            arcpy.FeatureClassToFeatureClass_conversion(ann_fc, out_dir, ann_name)
        label_shp = os.path.join(out_dir, ann_name)
        label_count = int(arcpy.GetCount_management(label_shp).getOutput(0))

    print(json.dumps({
        "status": "OK",
        "source_cad": cad_path,
        "counts": counts,
        "chosen_layer": chosen,
        "extracted_count": extracted_count,
        "output_shp": out_shp,
        "label_shp": label_shp,
        "label_count": label_count,
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
