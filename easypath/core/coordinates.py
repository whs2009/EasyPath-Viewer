from __future__ import annotations

def z_to_downsample(z: int | float, reference_z: int = 12) -> float:
    """Return downsample relative to EasyPath's 40x reference coordinate.

    With the default convention:

    * z=12 -> downsample 1   -> 40x
    * z=11 -> downsample 2   -> 20x
    * z=10 -> downsample 4   -> 10x
    * z=9  -> downsample 8   -> 5x
    """
    return float(2 ** (reference_z - float(z)))

def wsi_40x_to_native_xy(
    x_40x: float, y_40x: float, objective_power: float | None
) -> tuple[float, float]:
    """Convert EasyPath 40x reference XY to native level-0 slide XY.

    The original OpenSlide C++ wrapper used:

    ``scale_40_to_base = objective_power / 40``
    ``x_base = x_40x * scale_40_to_base``

    If ``objective_power`` is missing, we assume the native level-0 coordinate is
    already 40x-compatible and return the original values.
    """
    if objective_power is None:
        scale = 1.0
    else:
        scale = float(objective_power) / 40.0
    return float(x_40x) * scale, float(y_40x) * scale
