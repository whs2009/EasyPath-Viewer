from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from concurrent.futures import wait

from contextlib import ExitStack

import hashlib

import os

from pathlib import Path

import stat

import threading

from collections import OrderedDict

import numpy as np

from .ep_codec import decode_tile

from .ep_codec import decode_precision_tile

from .ep_codec import single_threaded_decode

from .ep_format import ENTRY

from .ep_format import HEADER

from .ep_format import MAGIC

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import MAX_REGION_PIXELS

from .ep_format import MAX_TILE_BYTES

from .ep_format import MAX_TILES

from .ep_format import TILE_SIZE

from .ep_format import SOLID_STORAGE

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import integer

from .ep_records import decode_record

from .ep_host_decode import host_decoder

DEFAULT_CACHE_BYTES = 32 * 1024 * 1024

MAX_CACHE_BYTES = 256 * 1024 * 1024

def _bounded_index_matches(index, payload_at, total):
    # The caller has verified the SHA and exact byte length of the immutable directory.
    # Prove every row in bounded batches; the scalar fallback preserves error order.
    rows = np.frombuffer(index, dtype="<u8").reshape(-1, 6)
    expected = payload_at
    for start in range(0, len(rows), 8192):
        block = rows[start:start + 8192]
        offsets, sizes = block[:, 0], block[:, 1]
        if (int(offsets[0]) != expected or np.any(offsets > total)
                or np.any(sizes < 2) or np.any(sizes > MAX_TILE_BYTES)):
            return False
        # At this point offsets and lengths are bounded well below uint64 overflow.
        ends = offsets + sizes
        if (np.any(ends > total) or not np.array_equal(offsets[1:], ends[:-1])
                or np.any((block[:, 2] | block[:, 3] | block[:, 4] | block[:, 5]) == 0)):
            return False
        expected = int(ends[-1])
    return expected == total

class EPFile:
    def __init__(self, path: str | Path, *, _pending: bool = False, cache_bytes: int = DEFAULT_CACHE_BYTES):
        integer(cache_bytes, "decoded cache budget", 0, MAX_CACHE_BYTES)
        self._open_stream(Path(path).open("rb", buffering=0), owns_stream=True, pending=_pending, cache_bytes=cache_bytes)

    @classmethod
    def from_stream(cls, stream, *, cache_bytes: int = DEFAULT_CACHE_BYTES) -> EPFile:
        """Borrow a regular-file descriptor; its admission owner closes it."""
        integer(cache_bytes, "decoded cache budget", 0, MAX_CACHE_BYTES)
        opened = cls.__new__(cls)
        opened._open_stream(stream, owns_stream=False, pending=False, cache_bytes=cache_bytes)
        return opened

    def _open_stream(self, stream, *, owns_stream, pending, cache_bytes, span=None):
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._cache_limit = 0 if pending else cache_bytes
        self._cache_size = self._cache_hits = self._cache_misses = 0
        self._file = stream
        self._owns_stream = owns_stream
        try:
            if not stream.readable() or not stream.seekable():
                raise EPError("EP source must be readable and seekable.")
            info = os.fstat(self._file.fileno())
            if not stat.S_ISREG(info.st_mode) or not HEADER.size <= info.st_size <= MAX_FILE_BYTES:
                raise EPError("EP source must be a bounded regular file.")
            self._identity = self._fingerprint(info)
            self._start, member_size = (0, info.st_size) if span is None else span
            integer(self._start, "member start", 0, info.st_size - HEADER.size)
            integer(member_size, "member length", HEADER.size, info.st_size - self._start)
            self._file.seek(self._start)
            raw = self._file.read(HEADER.size)
            if len(raw) != HEADER.size:
                raise EPError("EP header changed or was truncated while opening.")
            magic, major, minor, flags, size, count, index_at, total, meta_hash, index_hash, reserved = HEADER.unpack(raw)
            if (magic != MAGIC or (major, minor) not in ((1, 0), (2, 0)) or flags != (0 if pending else 1)
                    or reserved != bytes(16)):
                raise EPError("Unsupported or incomplete EP header.")
            if not 1 <= size <= MAX_METADATA or not 1 <= count <= MAX_TILES:
                raise EPError("EP metadata/index exceeds its bounds.")
            if total != member_size or index_at != HEADER.size + size:
                raise EPError("EP file length or index offset disagrees.")
            payload_at = index_at + count * ENTRY.size
            if payload_at > total:
                raise EPError("Truncated EP directory.")
            metadata = self._file.read(size)
            if hashlib.sha256(metadata).digest() != meta_hash:
                raise EPError("EP metadata checksum mismatch.")
            self._metadata = EPMetadata.from_bytes(metadata)
            self._host_decoder = None if pending else host_decoder()
            if (major == 2) != (self._metadata.storage_encoding == SOLID_STORAGE):
                raise EPError("EP header and storage metadata disagree.")
            self._storage_version = major
            if sum(plane.tiles for plane in self._metadata.planes) != count:
                raise EPError("EP tile count disagrees with its plane geometry.")
            self._index = self._file.read(count * ENTRY.size)
            if hashlib.sha256(self._index).digest() != index_hash:
                raise EPError("EP index checksum mismatch.")
            if len(self._index) != count * ENTRY.size:
                raise EPError("Truncated EP directory.")
            if count < 4096 or not _bounded_index_matches(self._index, payload_at, total):
                expected = payload_at
                for offset, length, digest in ENTRY.iter_unpack(self._index):
                    if offset != expected or not 2 <= length <= MAX_TILE_BYTES or length > total - offset:
                        raise EPError("EP tile offsets must cover one contiguous bounded payload area.")
                    if digest == bytes(32):
                        raise EPError("EP tile checksum is missing.")
                    expected += length
                if expected != total:
                    raise EPError("EP payload has an undeclared tail.")
            starts, count = [], 0
            for plane in self._metadata.planes:
                starts.append(count)
                count += plane.tiles
            self._starts = tuple(starts)
            self._check_identity()
        except BaseException:
            self.close()
            raise

    @classmethod
    def open(cls, path: str | Path) -> EPFile:
        return cls(path)

    @staticmethod
    def _fingerprint(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _check_identity(self):
        if self._file is None or self._file.closed:
            raise EPError("EP source is closed.")
        if self._fingerprint(os.fstat(self._file.fileno())) != self._identity:
            raise EPError("EP source changed while open.")

    def metadata(self) -> EPMetadata:
        with self._lock:
            self._check_identity()
            return self._metadata

    def _tile(self, plane: int, tx: int, ty: int) -> bytes:
        integer(plane, "plane", 0, len(self._metadata.planes) - 1)
        size = self._metadata.planes[plane]
        integer(tx, "tile x", 0, size.columns - 1)
        integer(ty, "tile y", 0, size.rows - 1)
        index = self._starts[plane] + ty * size.columns + tx
        offset, length, digest = ENTRY.unpack_from(self._index, index * ENTRY.size)
        with self._lock:
            self._check_identity()
            if not self._owns_stream:
                # Discard a borrowed Python stream's prefetch before checksum validation.
                self._file.seek(0, os.SEEK_END)
            self._file.seek(self._start + offset)
            payload = self._file.read(length)
            self._check_identity()
        if len(payload) != length or hashlib.sha256(payload).digest() != digest:
            raise EPError("EP tile payload checksum mismatch.")
        return payload

    def read_encoded_storage_tile(self, level: int, tx: int, ty: int) -> bytes:
        """Raw JXL in v1; typed JXL/solid record in v2, as declared by metadata."""
        integer(level, "level", 0, len(self._metadata.levels) - 1)
        return self._tile(level, tx, ty)

    def _decode(self, plane, tx, ty, *, cache=True):
        with self._lock:
            # Check encoded bytes even on hits: restored timestamps must not hide corruption.
            payload = self._tile(plane, tx, ty)
            key = (plane, tx, ty)
            if cache and key in self._cache:
                self._cache_hits += 1
                self._cache.move_to_end(key)
                return self._cache[key]
            self._cache_misses += 1
            pixels = self._decode_pixels(plane, tx, ty, payload)
            self._check_identity()
            if cache:
                self._remember(key, pixels)
            return pixels

    def _decode_pixels(self, plane, tx, ty, payload):
        if self._storage_version == 2:
            size = self._metadata.planes[plane]
            if self._host_decoder is not None and (self._metadata.channel_bits is None or plane >= len(self._metadata.levels)):
                return decode_record(payload, min(TILE_SIZE, size.width - tx*TILE_SIZE),
                    min(TILE_SIZE, size.height - ty*TILE_SIZE), native=self._host_decoder)
            return decode_record(payload, min(TILE_SIZE, size.width - tx*TILE_SIZE),
                min(TILE_SIZE, size.height - ty*TILE_SIZE),
                channel_bits=self._metadata.channel_bits if plane < len(self._metadata.levels) else None)
        if self._metadata.channel_bits is not None and plane < len(self._metadata.levels):
            return decode_precision_tile(payload, self._metadata.channel_bits)
        return decode_tile(payload) if self._host_decoder is None else decode_tile(payload, native=self._host_decoder)

    def _remember(self, key, pixels):
        if pixels.nbytes <= self._cache_limit:
            while self._cache and self._cache_size + pixels.nbytes > self._cache_limit:
                _, old = self._cache.popitem(last=False)
                self._cache_size -= old.nbytes
            pixels.flags.writeable = False
            self._cache[key] = pixels
            self._cache_size += pixels.nbytes

    def _decode_single(self, key, payload):
        with single_threaded_decode():
            return self._decode_pixels(*key, payload)

    def _decode_batch(self, keys, executor):
        if not 1 <= len(keys) <= 4 or len(set(keys)) != len(keys):
            raise EPError("EP decode batch must contain 1..4 distinct requested tiles.")
        if len(keys) == 1 or self._cache_limit < 4*TILE_SIZE*TILE_SIZE*3:
            return [self._decode(*key) for key in keys]
        with self._lock:
            # Four requested tiles at most; validate every payload, including hits.
            # Only pure decoding leaves this thread. File/cache ownership stays here.
            results = [None] * len(keys)
            missing = []
            for index, key in enumerate(keys):
                payload = self._tile(*key)
                if key in self._cache:
                    self._cache_hits += 1
                    self._cache.move_to_end(key)
                    results[index] = self._cache[key]
                else:
                    self._cache_misses += 1
                    missing.append((index, key, payload))
            for start in range(0, len(missing), 2):
                pair = missing[start:start+2]
                # Solid records need no codec worker; record validation still runs below.
                if len(pair) == 2 and (self._storage_version == 1 or
                                      all(item[2][:1] == b"\x00" for item in pair)):
                    first, second = pair
                    future = executor().submit(self._decode_single, first[1], first[2])
                    try:
                        right = self._decode_single(second[1], second[2])
                    except BaseException:
                        wait([future])
                        raise
                    decoded = (future.result(), right)
                else:
                    decoded = tuple(self._decode_pixels(*item[1], item[2]) for item in pair)
                for (index, key, _), pixels in zip(pair, decoded):
                    self._check_identity()
                    self._remember(key, pixels)
                    results[index] = pixels
            self._check_identity()
            return results

    def cache_info(self):
        with self._lock:
            self._check_identity()
            return dict(bytes=self._cache_size, limit_bytes=self._cache_limit,
                        tiles=len(self._cache), hits=self._cache_hits, misses=self._cache_misses)

    def clear_cache(self):
        with self._lock:
            self._check_identity()
            self._cache.clear()
            self._cache_size = self._cache_hits = self._cache_misses = 0

    def read_region(self, level: int, x: int, y: int, width: int, height: int,
                    out: np.ndarray | None = None) -> np.ndarray:
        integer(level, "level", 0, len(self._metadata.levels) - 1)
        return self._region(level, x, y, width, height, out)

    def _region(self, plane, x, y, width, height, out=None):
        integer(x, "x", -(1 << 53), 1 << 53)
        integer(y, "y", -(1 << 53), 1 << 53)
        integer(width, "width", 1, MAX_REGION_PIXELS)
        integer(height, "height", 1, MAX_REGION_PIXELS)
        if width * height > MAX_REGION_PIXELS:
            raise EPError("EP region exceeds its pixel budget.")
        if out is not None and (not isinstance(out, np.ndarray) or out.dtype != np.uint8 or
                out.shape != (height, width, 3) or not out.flags.c_contiguous or not out.flags.writeable):
            raise EPError("EP output buffer must be writable contiguous RGB8 with the requested shape.")
        with self._lock:
            self._check_identity()
        result = np.full((height, width, 3), 255, dtype=np.uint8) if out is None else out
        result.fill(255)
        size = self._metadata.planes[plane]
        left, top, right, bottom = max(0, x), max(0, y), min(size.width, x + width), min(size.height, y + height)
        if left < right and top < bottom:
            with ExitStack() as stack:
                pool = None
                def executor():
                    nonlocal pool
                    if pool is None:
                        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="ep-decode"))
                    return pool
                def copy_batch(keys):
                    for (_, tx, ty), pixels in zip(keys, self._decode_batch(keys, executor)):
                        ox, oy = tx * TILE_SIZE, ty * TILE_SIZE
                        x0, y0, x1, y1 = max(left, ox), max(top, oy), min(right, ox + TILE_SIZE), min(bottom, oy + TILE_SIZE)
                        result[y0-y:y1-y, x0-x:x1-x] = pixels[y0-oy:y1-oy, x0-ox:x1-ox]
                batch = []
                for ty in range(top // TILE_SIZE, (bottom - 1) // TILE_SIZE + 1):
                    for tx in range(left // TILE_SIZE, (right - 1) // TILE_SIZE + 1):
                        batch.append((plane, tx, ty))
                        if len(batch) == 4:
                            copy_batch(batch)
                            batch.clear()
                if batch:
                    copy_batch(batch)
        with self._lock:
            self._check_identity()
        return result

    def read_associated_image(self, name: str) -> np.ndarray:
        for index, plane in enumerate(self._metadata.associated, len(self._metadata.levels)):
            if plane.name == name:
                return self._region(index, 0, 0, plane.width, plane.height)
        raise KeyError(name)

    def validate_all_tiles(self, check_cancel=lambda: None, *, inspect_tile=None):
        for plane, size in enumerate(self._metadata.planes):
            for ty in range(size.rows):
                for tx in range(size.columns):
                    check_cancel()
                    pixels = self._decode(plane, tx, ty, cache=False)
                    valid_w, valid_h = min(TILE_SIZE, size.width - tx*TILE_SIZE), min(TILE_SIZE, size.height - ty*TILE_SIZE)
                    exact_padding = (self._metadata.profile == "rgb8-jxl-lossless-v1" or
                                     self._metadata.maximum_nrmse_percent == 0 or plane >= len(self._metadata.levels))
                    if exact_padding and (not np.all(pixels[valid_h:] == 255) or not np.all(pixels[:valid_h, valid_w:] == 255)):
                        raise EPError("EP edge padding is not white.")
                    if inspect_tile is not None:
                        inspect_tile(plane, tx*TILE_SIZE, ty*TILE_SIZE, pixels[:valid_h, :valid_w])

    def close(self):
        with self._lock:
            self._cache.clear()
            self._cache_size = 0
            if self._file is not None:
                if self._owns_stream:
                    self._file.close()
                self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
