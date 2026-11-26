from __future__ import annotations

import math

from typing import Any

def dicom_pyramid_options(series: Any, *, header_reader=None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Permit bounded spacing rounding only for a dimension-verified dyadic pyramid."""
    import pydicom

    levels = []
    for instance in series.instances:
        dataset = (header_reader or pydicom.dcmread)(
            instance.path, stop_before_pixels=True,
            specific_tags=["ImageType", "TotalPixelMatrixRows", "TotalPixelMatrixColumns",
                           "SharedFunctionalGroupsSequence", "ImagedVolumeWidth", "ImagedVolumeHeight",
                           "OpticalPathSequence", 0x77770010, 0x77771001],
        )
        if "VOLUME" not in list(getattr(dataset, "ImageType", [])):
            continue
        try:
            spacing = tuple(float(x) for x in dataset.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0].PixelSpacing)
            size = (int(dataset.TotalPixelMatrixRows), int(dataset.TotalPixelMatrixColumns))
            if len(spacing) != 2 or min(size) <= 0 or any(not math.isfinite(x) or x <= 0 for x in spacing):
                continue
        except (AttributeError, IndexError, TypeError, ValueError):
            continue
        levels.append((size, spacing, dataset))
    if not levels:
        return {}, {}
    levels.sort(key=lambda item: item[0][0] * item[0][1], reverse=True)
    base_size, base_spacing, base_dataset = levels[0]
    evidence: dict[str, Any] = {
        "dicom_pixel_spacing": list(base_spacing),
        "base_magnification_source": "inferred_from_pixel_spacing",
    }
    warnings: list[str] = []
    powers = {float(optical.ObjectiveLensPower) for optical in getattr(base_dataset, "OpticalPathSequence", [])
              if getattr(optical, "ObjectiveLensPower", None) is not None}
    if len(powers) == 1 and all(math.isfinite(value) and value > 0 for value in powers):
        evidence["reported_scan_magnification"] = powers.pop()
        evidence["base_magnification_source"] = "dicom_objective_lens_power"
    # A locally approved calibration may distinguish the image reference from
    # the optical lens. Read only our owned private block, never arbitrary tags.
    references = set()
    for _, _, dataset in levels:
        creator = dataset.get(0x77770010)
        reference = dataset.get(0x77771001)
        if creator is None or str(creator.value) != "EASYPATH_WSI_CALIBRATION" or reference is None:
            continue
        try:
            value = float(reference.value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid explicit DICOM image reference magnification") from exc
        if not math.isfinite(value) or not .1 <= value <= 1000:
            raise ValueError("Invalid explicit DICOM image reference magnification")
        references.add(value)
    if len(references) > 1:
        raise ValueError("DICOM pyramid has conflicting explicit image reference magnifications")
    if references:
        evidence["render_reference_magnification"] = references.pop()
        evidence["base_magnification_source"] = "explicit_dicom_image_reference"
    for key, pixels, spacing in (("ImagedVolumeWidth", base_size[1], base_spacing[1]),
                                 ("ImagedVolumeHeight", base_size[0], base_spacing[0])):
        declared = getattr(base_dataset, key, None)
        if declared is not None:
            value = float(declared)
            evidence[key] = value
            if not math.isfinite(value) or not math.isclose(value, pixels * spacing, rel_tol=.02, abs_tol=spacing):
                warnings.append(f"{key} conflicts with matrix dimensions and PixelSpacing; verify vendor calibration.")
    tolerance = .01
    verified = True
    for size, spacing, _ in levels:
        exponents = [math.log2(base / current) for base, current in zip(base_size, size)]
        level = round(sum(exponents) / 2)
        if level < 0 or any(abs(current - base / (2 ** level)) > 1 for current, base in zip(size, base_size)):
            verified = False
            break
        errors = [abs(math.log2(current / base) - level) for current, base in zip(spacing, base_spacing)]
        if max(errors) > .02:
            verified = False
            break
        tolerance = max(tolerance, *errors)
    options: dict[str, Any] = {}
    if verified and tolerance > .01:
        from wsidicom.config import Settings

        options["settings"] = Settings(level_scale_tolerance=.02)
        evidence["dicom_pyramid_spacing_tolerance"] = .02
        warnings.append("Rounded pyramid PixelSpacing accepted after checking matrix dimensions (level tolerance 0.02).")
    if warnings:
        evidence["metadata_warnings"] = warnings
    return options, evidence
