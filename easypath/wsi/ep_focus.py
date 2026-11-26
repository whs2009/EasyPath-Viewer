from __future__ import annotations

import hashlib

import os

import stat

import struct

import threading

from types import SimpleNamespace

from functools import partial

from pathlib import Path

from .ep_container import DEFAULT_CACHE_BYTES

from .ep_container import EPFile

from .ep_format import EPError

from .ep_format import ENTRY

from .ep_format import HEADER

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import MAX_TILES

from .ep_format import TILE_SIZE

from .ep_format import integer

from .focal_stack import FocusStack

STACK_MAGIC = b"EPZWSI\r\n"

HEAD = struct.Struct("<8sHHIQ32s")

ROW = struct.Struct("<dQQ32s")

COMPARE_BLOCK = 512 * 1024

INDEX_AND_RGB_BUDGET = MAX_TILES * ENTRY.size + DEFAULT_CACHE_BYTES

def _retained_rgb_limit(count, planes):
    available = INDEX_AND_RGB_BUDGET - count * ENTRY.size * planes - max(MAX_METADATA, 2 * COMPARE_BLOCK)
    return min(DEFAULT_CACHE_BYTES, available) if available >= 4 * TILE_SIZE**2 * 3 else 0

def _match_directory(member, header):
    # Exact comparison to bytes authenticated during open, including off-screen entries.
    f = member._file
    fields = HEADER.unpack(header)
    size, meta_hash = fields[4], fields[8]
    if hashlib.sha256(f.read(size)).digest() != meta_hash:
        raise EPError("Changed EP focus member metadata.")
    frozen = member._index
    for start in range(0, len(frozen), COMPARE_BLOCK):
        end = min(start + COMPARE_BLOCK, len(frozen))
        if f.read(end-start) != frozen[start:end]:
            raise EPError("Changed EP focus member index.")
    member._check_identity()

def open_ep_path(path, *, check_cancel=lambda: None):
    """Dispatch on the magic of the owned descriptor, without reopening by path."""
    stream = Path(path).open("rb",buffering=0)
    try:
        if stream.read(len(STACK_MAGIC)) == STACK_MAGIC:
            return EPFocusFile(stream=stream, owns_stream=True, check_cancel=check_cancel)
        opened=EPFile.__new__(EPFile)
        opened._open_stream(stream,owns_stream=True,pending=False,cache_bytes=DEFAULT_CACHE_BYTES)
        return opened
    except BaseException:
        stream.close()
        raise

class EPFocusFile:
    def __init__(self, path=None, *, stream=None, owns_stream=False, check_cancel=lambda: None):
        self.file = stream if stream is not None else Path(path).open("rb",buffering=0)
        self.owns_stream = stream is None or owns_stream
        self.lock = threading.RLock()
        self.active = None
        self._retained = []
        self._closed = False
        try:
            f = self.file
            f.seek(0)
            st = os.fstat(f.fileno())
            self.identity = EPFile._fingerprint(st)
            if not stat.S_ISREG(st.st_mode) or not HEAD.size <= st.st_size <= MAX_FILE_BYTES:
                raise EPError("Invalid EP focus file.")
            magic,version,has_z,n,total,checksum = HEAD.unpack(f.read(HEAD.size))
            if (magic,version) != (STACK_MAGIC,3) or has_z not in (0,1) or not 2 <= n <= 20 or total != st.st_size:
                raise EPError("Invalid EP focus header.")
            raw = f.read(n*ROW.size)
            if len(raw) != n*ROW.size or hashlib.sha256(raw).digest() != checksum:
                raise EPError("Invalid EP focus directory.")
            self.rows = tuple(ROW.iter_unpack(raw))
            at = HEAD.size + len(raw)
            zs, rasters = [], []
            expected_count = None
            for z,start,length,header_hash in self.rows:
                check_cancel()
                if (not has_z and z != 0 or start != at or not HEADER.size <= length <= total-at):
                    raise EPError("Invalid EP focus member range.")
                zs.append(z if has_z else None)
                f.seek(start)
                inner = f.read(HEADER.size)
                if len(inner) != HEADER.size or hashlib.sha256(inner).digest() != header_hash:
                    raise EPError("Changed EP focus member header.")
                count = HEADER.unpack(inner)[5]
                if expected_count is None:
                    expected_count = count
                    rgb_limit = _retained_rgb_limit(count, n)
                elif count != expected_count:
                    raise EPError("Focus plane tile counts differ.")
                member = self._member(start,length)
                if rgb_limit:
                    member._cache_limit = rgb_limit
                    self._retained.append(member)
                try:
                    rasters.append(member.metadata())
                finally:
                    if not rgb_limit:
                        member.close()
                del member
                at += length
            if at != total:
                raise EPError("Undeclared EP focus data.")
            self.stack = FocusStack(tuple(rasters),tuple(zs))
            self.stack.validate("ep")
            self._check()
        except BaseException:
            self.close()
            raise

    def _check(self):
        if self._closed or self.file.closed or self.identity != EPFile._fingerprint(os.fstat(self.file.fileno())):
            raise EPError("EP focus source changed or closed.")

    def _member(self,start,length):
        obj = EPFile.__new__(EPFile)
        obj._open_stream(self.file, owns_stream=False, pending=False,
                         cache_bytes=DEFAULT_CACHE_BYTES,span=(start,length))
        return obj

    def read_region(self,z,level,x,y,w,h):
        with self.lock:
            self._check()
            integer(z,"focus index",0,len(self.rows)-1)
            if self.active is None or self.active[0] != z:
                if self.active is not None:
                    if self._retained:
                        self.active[1].clear_cache()
                    else:
                        self.active[1].close()
                    self.active = None
                _,start,length,digest = self.rows[z]
                self.file.seek(0, os.SEEK_END)
                self.file.seek(start)
                header = self.file.read(HEADER.size)
                if hashlib.sha256(header).digest() != digest:
                    raise EPError("Changed focus member.")
                if self._retained:
                    member = self._retained[z]
                    _match_directory(member, header)
                else:
                    member = self._member(start,length)
                self.active = z,member
            return self.active[1].read_region(level,x,y,w,h)

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.active is not None and not self._retained:
                self.active[1].close()
            self.active = None
            for member in self._retained:
                member.close()
            self._retained.clear()
            if self.owns_stream:
                self.file.close()

    def metadata(self):
        self._check()
        return self.stack.rasters[0]

    def focus_view(self,z):
        integer(z,"focal index",0,len(self.rows)-1)
        return SimpleNamespace(metadata=self.metadata,read_region=partial(self.read_region,z))

    def clear_cache(self):
        with self.lock:
            self._check()
            if self.active: self.active[1].clear_cache()

    def cache_info(self):
        with self.lock:
            self._check()
            if self.active: return self.active[1].cache_info()
            return dict(limit_bytes=DEFAULT_CACHE_BYTES,bytes=0,tiles=0,hits=0,misses=0)
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
