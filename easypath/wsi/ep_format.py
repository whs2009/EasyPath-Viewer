from __future__ import annotations

import base64

import json

import math

import struct

from typing import Annotated

from typing import Literal

from pydantic import BaseModel

from pydantic import ConfigDict

from pydantic import Field

from pydantic import model_validator

from pydantic import model_serializer

MAGIC = b"EPWSI\x00\r\n"

HEADER = struct.Struct("<8sHHIQQQQ32s32s16s")

ENTRY = struct.Struct("<QQ32s")

TILE_SIZE = 512

MAX_METADATA = 2 * 1024 * 1024

MAX_TRANSFORM_SIDE = 500_000

MAX_TILES = 1_281_000

MAX_TILE_BYTES = 4 * 1024 * 1024

MAX_REGION_PIXELS = 16_777_216

MAX_FILE_BYTES = 1 << 40

SOLID_STORAGE = "jxl-solid-rgb-v2"

Dimension = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

class EPError(ValueError):
    """Invalid, unsupported or changed EP source."""

class TransformSizeLimit(EPError):
    """Geometry outside the selected output format's transform policy."""

def check_transform_dimensions(metadata, output_format):
    if output_format not in {"ep", "tiff", "dcm"}:
        raise ValueError("Unknown slide transformation output format.")
    base = metadata.levels[0]
    short, long = sorted((base.width, base.height))
    if output_format == "ep" and long > MAX_TRANSFORM_SIDE:
        raise TransformSizeLimit("EP supports up to 500,000 x 500,000 pixels.")
    if output_format != "ep" and (short > 100_000 or long > 200_000):
        raise TransformSizeLimit("TIFF and DICOM support up to 200,000 x 100,000 pixels in either orientation.")

class EPModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

class EPLevel(EPModel):
    width: Dimension
    height: Dimension

    @property
    def columns(self) -> int:
        return (self.width + TILE_SIZE - 1) // TILE_SIZE

    @property
    def rows(self) -> int:
        return (self.height + TILE_SIZE - 1) // TILE_SIZE

    @property
    def tiles(self) -> int:
        return self.columns * self.rows

class EPAssociated(EPLevel):
    name: Literal["overview", "macro", "label"]

class EPMetadata(EPModel):
    schema_version: Literal[1] = 1
    storage_encoding: Literal["jxl-solid-rgb-v2"] | None = None
    profile: Literal["rgb8-jxl-lossless-v1", "rgb8-jxl-bounded-lossy-v1", "rgb-jxl-precision-v1"] = "rgb8-jxl-lossless-v1"
    maximum_nrmse_percent: Annotated[int, Field(strict=True, ge=0, le=15)] | None = None
    channel_bits: Annotated[int, Field(strict=True, ge=5, le=7)] | None = None
    tile_size: Literal[512] = 512
    frames: Literal[1] = 1
    coordinate_contract: Literal["native-top-left-xy-v1"] = "native-top-left-xy-v1"
    color_space: Literal["sRGB"] = "sRGB"
    source_color_policy: Literal["declared-srgb", "icc-to-srgb"]
    source_icc_base64: Annotated[str, Field(max_length=1_398_104)] | None = None
    source_sha256: Digest | None = None
    objective_power: Annotated[float, Field(gt=0, le=200)] | None = None
    mpp_x: Annotated[float, Field(gt=0, le=1000)] | None = None
    mpp_y: Annotated[float, Field(gt=0, le=1000)] | None = None
    levels: Annotated[tuple[EPLevel, ...], Field(min_length=1, max_length=32)]
    associated: Annotated[tuple[EPAssociated, ...], Field(max_length=3)] = ()

    @model_validator(mode="before")
    @classmethod
    def strict_constants(cls, value):
        if isinstance(value, dict):
            for key in ("schema_version", "tile_size", "frames"):
                if key in value and type(value[key]) is not int:
                    raise ValueError("EP integer constants must be integers.")
        return value

    @model_validator(mode="after")
    def validate_geometry(self):
        if self.profile == "rgb-jxl-precision-v1":
            if self.channel_bits is None or self.maximum_nrmse_percent is None:
                raise ValueError("EP precision requires channel bits and an explicit compression limit.")
        elif self.channel_bits is not None:
            raise ValueError("Reduced channel precision requires its distinct EP profile.")
        elif self.profile == "rgb8-jxl-bounded-lossy-v1":
            if self.maximum_nrmse_percent is None or self.maximum_nrmse_percent == 0:
                raise ValueError("EP lossy profile requires its positive integer RGB RMSE limit.")
        elif self.maximum_nrmse_percent is not None:
            raise ValueError("EP lossless profile cannot declare a lossy limit.")
        # Literal equality alone accepts True/1 and 1.0/1 in some Pydantic paths.
        if any(type(value) is not int for value in (self.schema_version, self.tile_size, self.frames)):
            raise ValueError("EP integer constants must be integers.")
        for previous, current in zip(self.levels, self.levels[1:]):
            expected = ((previous.width + 1) // 2, (previous.height + 1) // 2)
            if (previous.width, previous.height) == (1, 1) or (current.width, current.height) != expected:
                raise ValueError("EP levels must preserve the ceil-dimension native 2x pyramid.")
        if len({plane.name for plane in self.associated}) != len(self.associated):
            raise ValueError("Duplicate EP associated-image role.")
        if any(plane.width * plane.height > MAX_REGION_PIXELS for plane in self.associated):
            raise ValueError("EP associated image exceeds the pixel limit.")
        base = self.levels[0]
        # Larger legacy aspect ratios remain readable with their original limit.
        tile_limit = MAX_TILES if max(base.width, base.height) <= MAX_TRANSFORM_SIDE else 1_000_000
        if sum(plane.tiles for plane in self.planes) > tile_limit:
            raise ValueError("EP index exceeds the tile limit.")
        if self.source_color_policy == "icc-to-srgb" and not self.source_icc_base64:
            raise ValueError("ICC conversion requires the preserved source profile.")
        if self.source_icc_base64 is not None:
            try:
                profile = base64.b64decode(self.source_icc_base64, validate=True)
            except ValueError as exc:
                raise ValueError("Invalid source ICC encoding.") from exc
            if not 128 <= len(profile) <= 1024 * 1024:
                raise ValueError("Source ICC profile exceeds its bounds.")
        return self

    @model_serializer(mode="wrap")
    def legacy_serialization(self, handler):
        result = handler(self)
        if self.maximum_nrmse_percent is None:
            result.pop("maximum_nrmse_percent", None)
        if self.channel_bits is None:
            result.pop("channel_bits", None)
        if self.storage_encoding is None:
            result.pop("storage_encoding", None)
        return result

    @property
    def planes(self) -> tuple[EPLevel, ...]:
        return self.levels + self.associated

    def to_bytes(self) -> bytes:
        data = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(data) > MAX_METADATA:
            raise EPError("EP metadata exceeds its byte limit.")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> EPMetadata:
        if not 1 <= len(data) <= MAX_METADATA:
            raise EPError("Invalid EP metadata length.")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise EPError("Duplicate EP metadata field.")
                result[key] = value
            return result
        def number(text):
            value = float(text)
            if not math.isfinite(value):
                raise EPError("Nonfinite EP metadata number.")
            return value
        def constant(_):
            raise EPError("Nonfinite EP metadata number.")
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                               parse_float=number, parse_constant=constant)
            if type(value) is not dict:
                raise EPError("EP metadata must be an object.")
            for key in ("schema_version", "tile_size", "frames"):
                if key in value and type(value[key]) is not int:
                    raise EPError("EP integer constants must be integers.")
            for key in ("levels", "associated"):
                if key in value:
                    if type(value[key]) is not list:
                        raise EPError("EP plane directories must be JSON arrays.")
                    value[key] = tuple(value[key])
            return cls.model_validate(value)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise EPError("Invalid EP metadata.") from exc

def pyramid_levels(width: int, height: int) -> tuple[EPLevel, ...]:
    """Native 2x levels through an overview-sized volume level."""
    levels = [EPLevel(width=width, height=height)]
    while max(width, height) > TILE_SIZE:
        width, height = (width + 1) // 2, (height + 1) // 2
        levels.append(EPLevel(width=width, height=height))
    return tuple(levels)

def integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise EPError(f"Invalid EP {name}.")
    return value
