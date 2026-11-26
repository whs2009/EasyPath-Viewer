from __future__ import annotations

import math

from pathlib import Path

import re

from typing import Any

import numpy as np

from PIL import Image

from PIL import TiffImagePlugin

def tiff_calibration_evidence(path: Path, metadata: Any) -> dict[str, Any]:
    """Retain declared scale evidence without silently changing render coordinates."""
    # Construct the TIFF plugin for header-only access. Image.open's whole-image
    # size check is inappropriate for a gigapixel pyramid; no base pixels load.
    with TiffImagePlugin.TiffImageFile(path) as source:
        description = str(source.tag_v2.get(270, ""))
    evidence: dict[str, Any] = {}
    for name, pattern in (("reported_description_mpp", r"(?:^|[,;\s])MPP\s*=\s*([0-9]+(?:\.[0-9]+)?)"),
                          ("reported_scan_magnification", r"(?:^|[,;\s])Lens\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*x")):
        found = re.search(pattern, description, re.IGNORECASE)
        if found and math.isfinite(float(found[1])) and float(found[1]) > 0:
            evidence[name] = float(found[1])
    declared_mpp = evidence.get("reported_description_mpp")
    reported_mpp = [value for value in (metadata.mpp_x, metadata.mpp_y) if value is not None and value > 0]
    if declared_mpp and any(not math.isclose(declared_mpp, value, rel_tol=.02) for value in reported_mpp):
        evidence["metadata_warnings"] = ["TIFF description MPP conflicts with TIFF resolution metadata; verify vendor calibration before physical measurements or scale-dependent AI."]
    declared_lens = evidence.get("reported_scan_magnification")
    reader_reference = getattr(metadata, "base_magnification", None)
    if declared_lens and reader_reference and not math.isclose(declared_lens, reader_reference, rel_tol=.02):
        evidence["base_magnification_source"] = "reader_image_reference"
    return evidence

def read_tiff_macro(path: Path, max_size: int) -> np.ndarray | None:
    """Decode only explicitly named, bounded-size associated TIFF pages."""
    if int(max_size) <= 0:
        raise ValueError("max_size must be positive")
    with TiffImagePlugin.TiffImageFile(path) as source:
        candidates = []
        for index in range(4096):
            try:
                source.seek(index)
            except EOFError:
                break
            name = str(source.tag_v2.get(270, "")).strip().casefold()
            if name not in {"macro", "overview", "label"}:
                continue
            if 322 in source.tag_v2 or source.width * source.height > 16 * 1024 * 1024:
                continue
            if source.mode not in {"RGB", "RGBA"}:
                continue
            candidates.append((0 if name in {"macro", "overview"} else 1, index))
        if not candidates:
            return None
        _, index = min(candidates)
        source.seek(index)
        # Only this previously bounded associated page is ever decoded.
        image = source.convert("RGB")
        image.thumbnail((int(max_size), int(max_size)), Image.Resampling.LANCZOS)
        return np.asarray(image, dtype=np.uint8)
