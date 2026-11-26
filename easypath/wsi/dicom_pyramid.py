from __future__ import annotations

from dataclasses import dataclass

import math

from typing import Any

import numpy as np

from PIL import Image

@dataclass(frozen=True)
class PhysicalLevel:
    level: Any
    pyramid: int
    downsample: float
    origin_x: float
    origin_y: float

def compatible_physical_levels(slide: Any) -> list[PhysicalLevel]:
    """Join only near-full-field levels with explicit, agreeing physical identity.

    wsidicom legitimately splits cropped levels when their origins differ. Keep
    those origins: never relax global grouping tolerances or merge arbitrary ROIs.
    Missing/ambiguous geometry retains the existing selected-pyramid behavior.
    """
    from wsidicom.geometry import PointMm

    pyramids = list(getattr(slide, "pyramids", []) or [])
    selected = list(slide.levels.levels)
    if len(pyramids) < 2 or not selected:
        return []
    base = selected[0]
    cs, spacing = base.image_coordinate_system, base.pixel_spacing
    if cs is None or spacing is None or not base.uids.frame_of_reference:
        return []
    entries = []
    used_scales = set()
    for index, pyramid in enumerate(pyramids):
        for level in pyramid.levels:
            target_cs, target_spacing = level.image_coordinate_system, level.pixel_spacing
            if (target_cs is None or target_spacing is None
                    or level.uids.study_instance != base.uids.study_instance
                    or level.uids.series_instance != base.uids.series_instance
                    or level.uids.frame_of_reference != base.uids.frame_of_reference
                    or level.optical_paths != base.optical_paths
                    or level.focal_planes != base.focal_planes
                    or target_cs.z_offset != cs.z_offset
                    or not math.isclose(target_cs.rotation, cs.rotation, abs_tol=1e-6)):
                continue
            dx = target_spacing.width / spacing.width
            dy = target_spacing.height / spacing.height
            if not all(math.isfinite(v) and v > 0 for v in (dx, dy)):
                continue
            exponent = round(math.log2(dx))
            if exponent < 0 or not math.isclose(dx, dy, rel_tol=1e-6) or not math.isclose(dx, 2**exponent, rel_tol=1e-6):
                continue
            offset = target_cs.slide_to_image(cs.image_to_slide(PointMm(0, 0)))
            ox, oy = offset.x / target_spacing.width, offset.y / target_spacing.height
            # At most four coarse pixels of border crop, never a separate field.
            errors = (ox, oy, level.size.width - (base.size.width / dx + ox),
                      level.size.height - (base.size.height / dy + oy))
            if any(not math.isfinite(v) or abs(v) > 4.01 for v in errors) or exponent in used_scales:
                continue
            if any(id(level) == id(v) for v in selected) or dx > max(v.pixel_spacing.width / spacing.width for v in selected):
                entries.append(PhysicalLevel(level, index, dx, ox, oy))
                used_scales.add(exponent)
    if not {id(v) for v in selected}.issubset({id(v.level) for v in entries}) or len(entries) <= len(selected):
        return []
    return sorted(entries, key=lambda v: v.downsample)

def read_physical_region(slide: Any, levels: list[PhysicalLevel], x: float, y: float,
                         downsample: float, width: int, height: int, focal_plane: float) -> np.ndarray:
    eligible = [v for v in levels if v.downsample <= downsample + 1e-9]
    entry = (eligible or levels[:1])[-1]
    scale = downsample / entry.downsample
    start_x, start_y = x / entry.downsample + entry.origin_x, y / entry.downsample + entry.origin_y
    left = max(0, math.floor(start_x) - 1)
    top = max(0, math.floor(start_y) - 1)
    right = min(entry.level.size.width, math.ceil(start_x + width * scale) + 1)
    bottom = min(entry.level.size.height, math.ceil(start_y + height * scale) + 1)
    if right <= left or bottom <= top:
        return np.full((height, width, 3), 255, dtype=np.uint8)
    image = slide.read_region((left, top), entry.level.level, (right-left, bottom-top),
                              z=focal_plane, pyramid=entry.pyramid, threads=1).convert("RGB")
    image = image.transform((width, height), Image.Transform.AFFINE,
                            (scale, 0, start_x-left, 0, scale, start_y-top),
                            Image.Resampling.BILINEAR, fillcolor=(255, 255, 255))
    return np.asarray(image, dtype=np.uint8)
