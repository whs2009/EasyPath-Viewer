from __future__ import annotations

from dataclasses import dataclass

from decimal import Decimal

import math

from .ep_format import EPError

from .ep_format import check_transform_dimensions

@dataclass(frozen=True)
class FocusStack:
    rasters: tuple
    positions_um: tuple

    def validate(self, fmt):
        if not 2 <= len(self.rasters) <= 20 or len(self.positions_um) != len(self.rasters):
            raise EPError("A scanner stack must describe every one of its 2 to 20 focal planes.")
        first = self.rasters[0]
        for raster in self.rasters:
            check_transform_dimensions(raster, fmt)
            if raster != first or raster.associated:
                raise EPError("Scanner planes require matching calibrated rasters and no unhandled associated images.")
        positions = self.positions_um
        if not all(z is None for z in positions):
            if not all(type(z) in (float, int) and math.isfinite(z) for z in positions) or len(set(positions)) != len(positions):
                raise EPError("Invalid or incomplete focal positions.")
        if fmt == "dcm":
            if None in positions or first.mpp_x is None or first.mpp_y is None:
                raise EPError("DICOM WSI requires measured XY calibration and focal positions.")
            # This writer uses TILED_FULL. Never approximate irregular positions.
            z = [Decimal(str(v)) for v in positions]
            if z[1] <= z[0] or any(b-a != z[1]-z[0] for a,b in zip(z,z[1:])):
                raise EPError("This DICOM WSI writer needs increasing, equally spaced focal positions.")
