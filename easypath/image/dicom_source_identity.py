from __future__ import annotations

from collections.abc import Sequence

import copy

import hashlib

import json

import re

SOURCE_EQUIPMENT_TAGS = (
    "Manufacturer", "ManufacturerModelName", "SoftwareVersions", "DeviceSerialNumber",
    "DeviceUID", "ContributingEquipmentSequence", "ImageType",
)

def identity_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()

def _text(value, issues, field):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 512 or any(ord(char) < 32 for char in value):
        issues.append(f"{field}:invalid")
        return None
    return value.strip() or None

def _sequence(value, issues, field):
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > 64:
        issues.append(f"{field}:invalid")
        return []
    return value

def _versions(value, issues, field):
    values = [value] if isinstance(value, str) else _sequence(value, issues, field)
    return [parsed for item in values if (parsed := _text(item, issues, field)) is not None]

def _equipment(dataset, issues, prefix):
    result = {
        "manufacturer": _text(getattr(dataset, "Manufacturer", None), issues, prefix + ".manufacturer"),
        "model": _text(getattr(dataset, "ManufacturerModelName", None), issues, prefix + ".model"),
        "software_versions": _versions(getattr(dataset, "SoftwareVersions", None), issues, prefix + ".software_versions"),
    }
    for name, tag in (("device_serial_sha256", "DeviceSerialNumber"), ("device_uid_sha256", "DeviceUID")):
        value = _text(getattr(dataset, tag, None), issues, prefix + "." + name)
        result[name] = hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None
    for name in ("manufacturer", "model", "software_versions"):
        if not result[name]:
            issues.append(f"{prefix}.{name}:unreported")
    return result

def extract_source_equipment(dataset) -> dict:
    """Keep producing equipment, contributing scanners and the last file writer distinct."""
    issues = []
    producer = _equipment(dataset, issues, "producer")
    contributors = []
    for index, item in enumerate(_sequence(getattr(dataset, "ContributingEquipmentSequence", None),
                                            issues, "contributors")):
        prefix = f"contributors/{index}"
        equipment = _equipment(item, issues, prefix)
        purposes = []
        for purpose in _sequence(getattr(item, "PurposeOfReferenceCodeSequence", None), issues, prefix + ".purpose"):
            purposes.append({name: _text(getattr(purpose, tag, None), issues, prefix + ".purpose." + name)
                             for name, tag in (("value", "CodeValue"), ("scheme", "CodingSchemeDesignator"),
                                               ("scheme_version", "CodingSchemeVersion"))})
        equipment["purposes"] = purposes
        contributors.append(equipment)
    image_type = _versions(getattr(dataset, "ImageType", None), issues, "image_type")
    acquisitions = [f"contributors/{index}" for index, equipment in enumerate(contributors)
                    if any(row["scheme"] == "DCM" and row["value"] == "109101" for row in equipment["purposes"])]
    if not acquisitions and image_type and image_type[0] == "ORIGINAL":
        acquisitions = ["producer"]
    if not acquisitions:
        issues.append("acquisition_equipment_unreported")
    meta = getattr(dataset, "file_meta", None)
    writer = {
        "implementation_class_uid": _text(getattr(meta, "ImplementationClassUID", None), issues, "file_writer.class_uid"),
        "implementation_version_name": _text(getattr(meta, "ImplementationVersionName", None), issues, "file_writer.version"),
    }
    uid = writer["implementation_class_uid"]
    if uid and (len(uid) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", uid)):
        writer["implementation_class_uid"] = None
        issues.append("file_writer.class_uid:invalid")
    for key, value in writer.items():
        if not value:
            issues.append(f"file_writer.{key}:unreported")
    unsigned = {"schema": "easypath.dicom-source-equipment.v1", "producer": producer,
                "contributors": contributors, "image_type": image_type, "file_writer": writer,
                "acquisition_equipment": acquisitions, "issues": sorted(set(issues))}
    return {**unsigned, "identity_sha256": identity_digest(unsigned)}

def series_source_equipment(series) -> dict:
    configurations, instances = {}, []
    complete = True
    for instance in series.instances:
        config = copy.deepcopy(getattr(instance, "source_equipment", None) or extract_source_equipment(None))
        digest = config.pop("identity_sha256", None)
        if digest != identity_digest(config):
            raise ValueError("DICOM source equipment evidence identity is invalid.")
        config["identity_sha256"] = digest
        configurations[digest] = config
        complete = complete and not config["issues"]
        instances.append({"source_id": instance.source_id, "content_sha256": instance.content_sha256,
                          "equipment_identity_sha256": digest})
    unsigned = {"schema": "easypath.dicom-series-equipment.v1",
                "configurations": [configurations[key] for key in sorted(configurations)],
                "instances": instances, "complete": bool(instances) and complete,
                "mixed": len(configurations) > 1}
    return {**unsigned, "identity_sha256": identity_digest(unsigned)}
