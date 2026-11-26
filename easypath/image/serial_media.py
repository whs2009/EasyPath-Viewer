from __future__ import annotations

from dataclasses import dataclass

from dataclasses import field

import hashlib

import io

import os

from pathlib import Path

import re

from typing import Any

from typing import Iterable

from typing import Mapping

import numpy as np

from easypath.checkpoint_io import native_file_path

from easypath.image.dicom_intake_stream import BLOCK_SIZE

from easypath.image.dicom_intake_stream import DicomSourceChangedError

from easypath.image.dicom_intake_stream import VerifiedHeaderStream

from easypath.image.dicom_source_identity import SOURCE_EQUIPMENT_TAGS

from easypath.image.dicom_source_identity import extract_source_equipment

DICOM_WSI_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.77.1.6"

_DICOM_UID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")

@dataclass(frozen=True)
class DicomInstance:
    path: Path
    study_uid: str
    series_uid: str
    series_key: str
    sop_instance_uid: str
    sop_class_uid: str
    modality: str
    series_number: int | None
    series_description: str
    protocol_name: str
    body_part_examined: str
    frame_of_reference_uid: str
    transfer_syntax_uid: str
    rows: int
    columns: int
    samples_per_pixel: int
    photometric_interpretation: str
    bits_allocated: int
    bits_stored: int
    pixel_representation: int
    pixel_spacing: tuple[float, ...] | None
    slice_thickness_mm: float | None
    spacing_between_slices_mm: float | None
    orientation: tuple[float, ...] | None
    frame_count: int
    instance_number: int
    slice_position: float | None
    native_frame_order: tuple[int, ...]
    frame_positions: tuple[float | None, ...]
    frame_image_positions: tuple[tuple[float, ...] | None, ...]
    frame_orientations: tuple[tuple[float, ...] | None, ...]
    frame_pixel_spacings: tuple[tuple[float, ...] | None, ...]
    content_sha256: str
    source_id: str
    source_equipment: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class DicomSeries:
    series_key: str
    legacy_series_key: str
    study_uid: str
    series_uid: str
    input_kind: str
    modality: str
    sop_class_uid: str
    instances: tuple[DicomInstance, ...]
    frame_map: tuple[tuple[int, int], ...]
    ordering_method: str
    ordering_values: tuple[float | int | str | None, ...]
    ordering_warnings: tuple[str, ...]
    source_inventory_sha256: str
    compatibility_sha256: str
    transfer_syntax_uids: tuple[str, ...]

    @property
    def frame_count(self) -> int:
        return len(self.frame_map)

    @property
    def width_px(self) -> int:
        return max((item.columns for item in self.instances), default=1)

    @property
    def height_px(self) -> int:
        return max((item.rows for item in self.instances), default=1)

    @property
    def source_file_count(self) -> int:
        return len(self.instances)

def _dicom_dependency() -> Any:
    try:
        import pydicom
    except ImportError as exc:  # pragma: no cover - installation contract covers this branch.
        raise RuntimeError("DICOM media requires the pydicom runtime.") from exc
    return pydicom

def _series_key(series_uid: str) -> str:
    return hashlib.sha256(series_uid.encode("utf-8", errors="replace")).hexdigest()[:24]

def _valid_uid(value: str) -> bool:
    return bool(value and len(value) <= 64 and _DICOM_UID_RE.fullmatch(value))

def _float_values(value: Any, expected: int) -> tuple[float, ...] | None:
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return values if len(values) >= expected else None

def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None

def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _tolerant_geometry_signature(
    values: tuple[float, ...] | None,
    *,
    decimals: int = 4,
) -> tuple[float, ...]:
    """Group harmless DICOM decimal serialization noise without hiding geometry changes."""

    return tuple(round(float(value), decimals) for value in values or ())

def _slice_position(dataset: Any) -> float | None:
    orientation = _float_values(getattr(dataset, "ImageOrientationPatient", None), 6)
    position = _float_values(getattr(dataset, "ImagePositionPatient", None), 3)
    if orientation is None or position is None:
        return None
    row = orientation[:3]
    column = orientation[3:6]
    normal = (
        row[1] * column[2] - row[2] * column[1],
        row[2] * column[0] - row[0] * column[2],
        row[0] * column[1] - row[1] * column[0],
    )
    return sum(normal[index] * position[index] for index in range(3))

def _projected_position(orientation_value: Any, position_value: Any) -> float | None:
    orientation = _float_values(orientation_value, 6)
    position = _float_values(position_value, 3)
    if orientation is None or position is None:
        return None
    row = orientation[:3]
    column = orientation[3:6]
    normal = (
        row[1] * column[2] - row[2] * column[1],
        row[2] * column[0] - row[0] * column[2],
        row[0] * column[1] - row[1] * column[0],
    )
    return sum(normal[index] * position[index] for index in range(3))

def _sequence_first(value: Any) -> Any | None:
    try:
        return value[0] if value else None
    except (TypeError, IndexError):
        return None

def _enhanced_frame_geometry(
    dataset: Any,
    frame_count: int,
) -> tuple[
    tuple[float | None, ...],
    tuple[tuple[float, ...] | None, ...],
    tuple[tuple[float, ...] | None, ...],
    tuple[tuple[float, ...] | None, ...],
]:
    if frame_count <= 1:
        return (
            (_slice_position(dataset),),
            (_float_values(getattr(dataset, "ImagePositionPatient", None), 3),),
            (_float_values(getattr(dataset, "ImageOrientationPatient", None), 6),),
            (_float_values(getattr(dataset, "PixelSpacing", None), 2),),
        )
    shared = _sequence_first(getattr(dataset, "SharedFunctionalGroupsSequence", None))
    shared_orientation = None
    shared_spacing = None
    if shared is not None:
        orientation_item = _sequence_first(getattr(shared, "PlaneOrientationSequence", None))
        if orientation_item is not None:
            shared_orientation = getattr(orientation_item, "ImageOrientationPatient", None)
        pixel_item = _sequence_first(getattr(shared, "PixelMeasuresSequence", None))
        if pixel_item is not None:
            shared_spacing = getattr(pixel_item, "PixelSpacing", None)
    default_orientation = getattr(dataset, "ImageOrientationPatient", None) or shared_orientation
    default_spacing = getattr(dataset, "PixelSpacing", None) or shared_spacing
    default_position = getattr(dataset, "ImagePositionPatient", None)
    positions: list[float | None] = []
    image_positions: list[tuple[float, ...] | None] = []
    orientations: list[tuple[float, ...] | None] = []
    spacings: list[tuple[float, ...] | None] = []
    groups = list(getattr(dataset, "PerFrameFunctionalGroupsSequence", None) or [])
    for frame_index in range(frame_count):
        group = groups[frame_index] if frame_index < len(groups) else None
        orientation = default_orientation
        spacing = default_spacing
        position = default_position
        if group is not None:
            orientation_item = _sequence_first(getattr(group, "PlaneOrientationSequence", None))
            if orientation_item is not None:
                orientation = getattr(orientation_item, "ImageOrientationPatient", None) or orientation
            position_item = _sequence_first(getattr(group, "PlanePositionSequence", None))
            if position_item is not None:
                position = getattr(position_item, "ImagePositionPatient", None)
            pixel_item = _sequence_first(getattr(group, "PixelMeasuresSequence", None))
            if pixel_item is not None:
                spacing = getattr(pixel_item, "PixelSpacing", None) or spacing
        image_position = _float_values(position, 3)
        positions.append(_projected_position(orientation, image_position))
        image_positions.append(image_position)
        orientations.append(_float_values(orientation, 6))
        spacings.append(_float_values(spacing, 2))
    return tuple(positions), tuple(image_positions), tuple(orientations), tuple(spacings)

def _sha256_file(path: Path, *, block_hashes: list[str] | None = None) -> str:
    digest = hashlib.sha256()
    with native_file_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK_SIZE), b""):
            digest.update(chunk)
            if block_hashes is not None:
                block_hashes.append(hashlib.sha256(chunk).hexdigest())
    return digest.hexdigest()

def _source_id(candidate: Path, source: Path) -> str:
    try:
        relative = candidate.relative_to(source) if candidate != source else Path(candidate.name)
    except ValueError:
        relative = Path(candidate.name)
    digest = hashlib.sha256(relative.as_posix().encode("utf-8", errors="replace")).hexdigest()
    return f"dicom-source-{digest[:24]}"

def _candidate_dicom_files(path: Path) -> Iterable[Path]:
    native = native_file_path(path, force_extended=True)
    if native.is_file():
        yield path
        return
    for candidate in sorted(native.rglob("*"), key=lambda item: str(item).casefold()):
        if candidate.is_file() and not candidate.name.startswith("."):
            yield path / candidate.relative_to(native)

def discover_dicom_series(path: str | Path) -> list[DicomSeries]:
    source = Path(path).resolve()
    report = discover_dicom_series_report(source)
    series = list(report["series"])
    if not series:
        quarantined = int(report.get("quarantined_count") or 0)
        detail = f"; {quarantined} instance(s) were quarantined" if quarantined else ""
        raise ValueError(f"No admissible DICOM series were found in {source}{detail}.")
    return series

def _source_inventory(
    candidates: Iterable[Path],
    source: Path,
) -> tuple[str, list[dict[str, Any]], dict[Path, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    by_path: dict[Path, dict[str, Any]] = {}
    for candidate in candidates:
        stat = native_file_path(candidate).stat()
        blocks: list[str] = []
        row = {
            "source_id": _source_id(candidate, source),
            "size_bytes": int(stat.st_size),
            "sha256": _sha256_file(candidate, block_hashes=blocks),
        }
        rows.append(row)
        signature = _dicom_source_stat_signature(stat)
        if _dicom_source_stat_signature(native_file_path(candidate).stat()) != signature:
            raise ValueError("DICOM source changed during discovery. Retry the source scan.")
        by_path[candidate] = {**row, "stat_signature": signature, "block_hashes": blocks}
    compact = "\n".join(
        f"{row['source_id']}\0{row['size_bytes']}\0{row['sha256']}" for row in rows
    ).encode("utf-8")
    return hashlib.sha256(compact).hexdigest(), rows, by_path

def _dicom_source_stat_signature(stat: os.stat_result) -> tuple[int, ...]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

def discover_dicom_series_report(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not native_file_path(source).exists():
        raise FileNotFoundError(source)
    candidates = list(_candidate_dicom_files(source))
    inventory_sha256, inventory, inventory_by_path = _source_inventory(candidates, source)
    series, quarantined = _discover_dicom_series_uncached(
        source,
        candidates,
        inventory_sha256,
        inventory_by_path,
    )
    return {
        "schema": "easypath.dicom-discovery.v2",
        "source": str(source),
        "source_inventory_sha256": inventory_sha256,
        "source_file_count": len(inventory),
        "source_inventory": inventory,
        "series": series,
        "quarantined": quarantined,
        "quarantined_count": len(quarantined),
    }

def _discover_dicom_series_uncached(
    source: Path,
    candidates: Iterable[Path] | None = None,
    inventory_sha256: str = "",
    inventory_by_path: Mapping[Path, Mapping[str, Any]] | None = None,
    *, header_reader=None,
) -> tuple[list[DicomSeries], list[dict[str, Any]]]:
    pydicom = _dicom_dependency()
    grouped: dict[tuple[str, str], list[DicomInstance]] = {}
    quarantined: list[dict[str, Any]] = []
    seen_sop_instances: dict[str, dict[str, Any]] = {}
    conflicting_sop_instances: set[str] = set()
    if header_reader is not None and (candidates is None or inventory_by_path is None):
        raise ValueError("Descriptor-based DICOM discovery requires a complete explicit inventory.")
    for candidate in candidates if candidates is not None else _candidate_dicom_files(source):
        # Inventory identity is the enumerated path, not a second resolution of
        # its parents. Bytes and header blocks are still verified independently.
        inventory_row = dict((inventory_by_path or {}).get(candidate) or {})
        if header_reader is not None and (not inventory_row.get("source_id") or not inventory_row.get("sha256")):
            raise ValueError("Descriptor-based DICOM discovery cannot infer undeclared source evidence.")
        source_id = str(inventory_row.get("source_id") or _source_id(candidate, source))
        signature = None
        if header_reader is None:
            signature = _dicom_source_stat_signature(native_file_path(candidate).stat())
            if inventory_row.get("stat_signature", signature) != signature:
                raise ValueError("DICOM source changed during discovery. Retry the source scan.")
        content_sha256 = str(inventory_row.get("sha256") or "")
        if header_reader is None:
            blocks = inventory_row.get("block_hashes")
            if blocks is None:
                blocks = []
                current_hash = _sha256_file(candidate, block_hashes=blocks)
                if content_sha256 and current_hash != content_sha256:
                    raise DicomSourceChangedError()
                content_sha256 = current_hash
            def read_header(path, **kwargs):
                with native_file_path(path).open("rb") as stream:
                    with io.BufferedReader(VerifiedHeaderStream(stream, blocks, signature[2])) as verified:
                        dataset = pydicom.dcmread(verified, **kwargs)
                        verified.raw.verify_read_blocks()
                        return dataset
        else:
            read_header = header_reader
        try:
            dataset = read_header(
                candidate if header_reader is not None else native_file_path(candidate),
                stop_before_pixels=True,
                specific_tags=[
                    *SOURCE_EQUIPMENT_TAGS,
                    "StudyInstanceUID",
                    "SeriesInstanceUID",
                    "SOPInstanceUID",
                    "SOPClassUID",
                    "Modality",
                    "SeriesNumber",
                    "SeriesDescription",
                    "ProtocolName",
                    "BodyPartExamined",
                    "FrameOfReferenceUID",
                    "Rows",
                    "Columns",
                    "SamplesPerPixel",
                    "PhotometricInterpretation",
                    "BitsAllocated",
                    "BitsStored",
                    "PixelRepresentation",
                    "PixelSpacing",
                    "SliceThickness",
                    "SpacingBetweenSlices",
                    "NumberOfFrames",
                    "InstanceNumber",
                    "ImageOrientationPatient",
                    "ImagePositionPatient",
                    "SharedFunctionalGroupsSequence",
                    "PerFrameFunctionalGroupsSequence",
                ],
            )
        except DicomSourceChangedError:
            raise
        except Exception as exc:
            if signature is not None and _dicom_source_stat_signature(native_file_path(candidate).stat()) != signature:
                raise ValueError("DICOM source changed during discovery. Retry the source scan.") from exc
            quarantined.append(
                {
                    "source_id": source_id,
                    "source_sha256": content_sha256,
                    "reasons": ["unreadable_dicom"],
                    "detail": type(exc).__name__,
                }
            )
            continue
        if signature is not None and _dicom_source_stat_signature(native_file_path(candidate).stat()) != signature:
            raise ValueError("DICOM source changed during discovery. Retry the source scan.")
        study_uid = str(getattr(dataset, "StudyInstanceUID", "") or "").strip()
        series_uid = str(getattr(dataset, "SeriesInstanceUID", "") or "").strip()
        sop_instance_uid = str(getattr(dataset, "SOPInstanceUID", "") or "").strip()
        sop_class_uid = str(getattr(dataset, "SOPClassUID", "") or "").strip()
        missing = [
            name
            for name, value in (
                ("StudyInstanceUID", study_uid),
                ("SeriesInstanceUID", series_uid),
                ("SOPInstanceUID", sop_instance_uid),
                ("SOPClassUID", sop_class_uid),
            )
            if not _valid_uid(value)
        ]
        if missing:
            quarantined.append(
                {
                    "source_id": source_id,
                    "source_sha256": content_sha256,
                    "reasons": ["missing_or_malformed_uid"],
                    "missing_or_malformed": missing,
                    "fallback_identity": hashlib.sha256(
                        f"{study_uid}|{sop_instance_uid}|{source_id}".encode("utf-8", errors="replace")
                    ).hexdigest()[:24],
                }
            )
            continue
        previous = seen_sop_instances.get(sop_instance_uid)
        if previous is not None:
            uid_hash = hashlib.sha256(sop_instance_uid.encode("ascii")).hexdigest()
            if content_sha256 != previous["sha256"] and sop_instance_uid not in conflicting_sop_instances:
                conflicting_sop_instances.add(sop_instance_uid)
                # A later conflicting copy invalidates the first copy and any
                # earlier identical duplicates. Directory order cannot pick a winner.
                first = {"source_id": previous["source_id"], "source_sha256": previous["sha256"],
                         "reasons": ["conflicting_sop_instance_uid"], "sop_instance_uid_sha256": uid_hash}
                quarantined.append(first)
                for row in previous["duplicates"]:
                    row["reasons"] = ["conflicting_sop_instance_uid"]
            row = {
                "source_id": source_id, "source_sha256": content_sha256,
                "reasons": ["conflicting_sop_instance_uid" if sop_instance_uid in conflicting_sop_instances
                            else "duplicate_sop_instance_uid"],
                "sop_instance_uid_sha256": uid_hash, "first_source_id": previous["source_id"],
            }
            quarantined.append(row)
            previous["duplicates"].append(row)
            continue
        seen_sop_instances[sop_instance_uid] = {"source_id": source_id, "sha256": content_sha256, "duplicates": []}
        key = _series_key(f"{study_uid}|{series_uid}")
        frame_count = max(1, int(getattr(dataset, "NumberOfFrames", 1) or 1))
        (
            frame_positions,
            frame_image_positions,
            frame_orientations,
            frame_pixel_spacings,
        ) = _enhanced_frame_geometry(dataset, frame_count)
        complete_positions = (
            len(frame_positions) == frame_count
            and all(value is not None for value in frame_positions)
            and len({round(float(value), 6) for value in frame_positions if value is not None}) == frame_count
        )
        native_frame_order = tuple(
            sorted(range(frame_count), key=lambda index: float(frame_positions[index] or 0.0))
            if complete_positions
            else range(frame_count)
        )
        file_meta = getattr(dataset, "file_meta", None)
        instance = DicomInstance(
            path=candidate,
            study_uid=study_uid,
            series_uid=series_uid,
            series_key=key,
            sop_instance_uid=sop_instance_uid,
            sop_class_uid=sop_class_uid,
            modality=str(getattr(dataset, "Modality", "") or "DICOM").upper(),
            series_number=_optional_int(getattr(dataset, "SeriesNumber", None)),
            series_description=str(getattr(dataset, "SeriesDescription", "") or "").strip(),
            protocol_name=str(getattr(dataset, "ProtocolName", "") or "").strip(),
            body_part_examined=str(getattr(dataset, "BodyPartExamined", "") or "").strip(),
            frame_of_reference_uid=str(getattr(dataset, "FrameOfReferenceUID", "") or ""),
            transfer_syntax_uid=str(getattr(file_meta, "TransferSyntaxUID", "") or ""),
            rows=max(1, int(getattr(dataset, "Rows", 1) or 1)),
            columns=max(1, int(getattr(dataset, "Columns", 1) or 1)),
            samples_per_pixel=max(1, int(getattr(dataset, "SamplesPerPixel", 1) or 1)),
            photometric_interpretation=str(getattr(dataset, "PhotometricInterpretation", "") or "").upper(),
            bits_allocated=max(1, int(getattr(dataset, "BitsAllocated", 8) or 8)),
            bits_stored=max(1, int(getattr(dataset, "BitsStored", 8) or 8)),
            pixel_representation=int(getattr(dataset, "PixelRepresentation", 0) or 0),
            pixel_spacing=_float_values(getattr(dataset, "PixelSpacing", None), 2),
            slice_thickness_mm=_optional_float(getattr(dataset, "SliceThickness", None)),
            spacing_between_slices_mm=_optional_float(
                getattr(dataset, "SpacingBetweenSlices", None)
            ),
            orientation=_float_values(getattr(dataset, "ImageOrientationPatient", None), 6),
            frame_count=frame_count,
            instance_number=int(getattr(dataset, "InstanceNumber", 0) or 0),
            slice_position=_slice_position(dataset),
            native_frame_order=native_frame_order,
            frame_positions=frame_positions,
            frame_image_positions=frame_image_positions,
            frame_orientations=frame_orientations,
            frame_pixel_spacings=frame_pixel_spacings,
            content_sha256=content_sha256,
            source_id=source_id,
            source_equipment=extract_source_equipment(dataset),
        )
        grouped.setdefault((study_uid, series_uid), []).append(instance)
    if not grouped:
        return [], quarantined

    discovered: list[DicomSeries] = []
    for (study_uid, series_uid), instances in grouped.items():
        instances = [item for item in instances if item.sop_instance_uid not in conflicting_sop_instances]
        if not instances:
            continue
        compatible: dict[tuple[Any, ...], list[DicomInstance]] = {}
        for item in instances:
            signature = (
                item.sop_class_uid,
                item.modality,
                item.frame_of_reference_uid,
                item.rows,
                item.columns,
                item.samples_per_pixel,
                item.photometric_interpretation,
                item.bits_allocated,
                item.bits_stored,
                item.pixel_representation,
                _tolerant_geometry_signature(item.pixel_spacing),
                _tolerant_geometry_signature(item.orientation),
            )
            if item.sop_class_uid == DICOM_WSI_SOP_CLASS_UID:
                # A WSI pyramid includes differently sized tiles, labels and
                # overviews. Radiology geometry partitions do not apply to it;
                # wsidicom validates the pyramid/optical-path compatibility.
                signature = (item.sop_class_uid, item.modality, item.frame_of_reference_uid)
            compatible.setdefault(signature, []).append(item)
        for signature_index, (signature, partition) in enumerate(compatible.items(), start=1):
            positions = [item.slice_position for item in partition if item.slice_position is not None]
            position_order_is_complete = (
                len(positions) == len(partition)
                and len({round(float(value), 6) for value in positions}) == len(partition)
            )
            if position_order_is_complete:
                ordered = sorted(partition, key=lambda item: (float(item.slice_position or 0.0), item.path.name.casefold()))
                ordering_method = "orientation_position_projection"
            elif all(item.instance_number > 0 for item in partition):
                ordered = sorted(partition, key=lambda item: (item.instance_number, item.path.name.casefold()))
                ordering_method = "instance_number"
            else:
                ordered = sorted(partition, key=lambda item: item.path.name.casefold())
                ordering_method = "stable_filename"

            frame_refs: list[tuple[int, int, float | int | str | None]] = []
            for instance_index, item in enumerate(ordered):
                for native_index in item.native_frame_order:
                    position = item.frame_positions[native_index] if native_index < len(item.frame_positions) else None
                    evidence: float | int | str | None = position
                    if evidence is None and item.instance_number > 0:
                        evidence = item.instance_number
                    if evidence is None:
                        evidence = item.path.name
                    frame_refs.append((instance_index, native_index, evidence))
            if frame_refs and all(isinstance(item[2], float) for item in frame_refs):
                frame_refs.sort(key=lambda item: float(item[2]))

            warnings: list[str] = []
            numeric_positions = [float(item[2]) for item in frame_refs if isinstance(item[2], float)]
            rounded_positions = [round(value, 6) for value in numeric_positions]
            if len(set(rounded_positions)) != len(rounded_positions):
                warnings.append("duplicate_slice_positions")
            if len(numeric_positions) >= 3:
                gaps = [round(numeric_positions[index + 1] - numeric_positions[index], 6) for index in range(len(numeric_positions) - 1)]
                if len(set(gaps)) > 1:
                    warnings.append("nonuniform_slice_spacing")
            if ordering_method != "orientation_position_projection":
                warnings.append(f"ordering_fallback:{ordering_method}")
            if len(compatible) > 1:
                warnings.append("source_series_split_by_compatibility")
            spacing_values = {
                tuple(round(float(value), 8) for value in item.pixel_spacing or ())
                for item in partition
            }
            orientation_values = {
                tuple(round(float(value), 8) for value in item.orientation or ())
                for item in partition
            }
            if len(spacing_values) > 1:
                warnings.append("pixel_spacing_normalized_with_tolerance")
            if len(orientation_values) > 1:
                warnings.append("orientation_normalized_with_tolerance")

            signature_text = repr(signature).encode("utf-8")
            compatibility_sha256 = hashlib.sha256(signature_text).hexdigest()
            base_identity = f"{study_uid}|{series_uid}"
            if len(compatible) > 1:
                base_identity += f"|{compatibility_sha256}"
            key = _series_key(base_identity)
            sop_class_uid = str(signature[0] or "")
            modality = str(signature[1] or "DICOM")
            discovered.append(
                DicomSeries(
                    series_key=key,
                    legacy_series_key=_series_key(series_uid),
                    study_uid=study_uid,
                    series_uid=series_uid,
                    input_kind="wsi" if sop_class_uid == DICOM_WSI_SOP_CLASS_UID else "image",
                    modality=modality,
                    sop_class_uid=sop_class_uid,
                    instances=tuple(ordered),
                    frame_map=tuple((item[0], item[1]) for item in frame_refs),
                    ordering_method=ordering_method,
                    ordering_values=tuple(item[2] for item in frame_refs),
                    ordering_warnings=tuple(warnings),
                    source_inventory_sha256=inventory_sha256,
                    compatibility_sha256=compatibility_sha256,
                    transfer_syntax_uids=tuple(
                        sorted({str(item.transfer_syntax_uid or "") for item in ordered})
                    ),
                )
            )
    return sorted(discovered, key=lambda item: (item.modality, item.study_uid, item.series_key)), quarantined

def select_dicom_series(path: str | Path, series_key: str | None = None) -> DicomSeries:
    series = discover_dicom_series(path)
    requested = str(series_key or "").strip()
    if requested:
        for item in series:
            if item.series_key == requested or item.legacy_series_key == requested:
                return item
        raise ValueError(f"DICOM series {requested} was not found in {path}.")
    if len(series) != 1:
        raise ValueError(f"DICOM source contains {len(series)} series; a series identity is required.")
    return series[0]
