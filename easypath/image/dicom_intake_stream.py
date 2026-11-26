from __future__ import annotations

import hashlib

import io

BLOCK_SIZE = 1024 * 1024

class DicomSourceChangedError(ValueError):
    def __init__(self) -> None:
        super().__init__("DICOM source changed during discovery. Retry the source scan.")

class VerifiedHeaderStream(io.RawIOBase):
    """Seekable read-only adapter retaining at most one verified source block.

    The caller owns the underlying stream. Only bytes matching the original
    discovery inventory reach the parser, even if a same-size rewrite has the
    same filesystem timestamp. Unread pixel blocks are checked at import copy.
    """

    def __init__(self, source, block_hashes: list[str], size: int) -> None:
        super().__init__()
        if size < 0 or len(block_hashes) != (size + BLOCK_SIZE - 1) // BLOCK_SIZE:
            raise ValueError("Invalid DICOM header block inventory.")
        self._source = source
        # BufferedReader forwards this attribute; pydicom requires it even for
        # an in-memory source that has no filename.
        self.name = getattr(source, "name", None)
        self._hashes = tuple(block_hashes)
        self._size = size
        self._position = 0
        self._block_index = -1
        self._block = b""
        self._read_blocks: set[int] = set()

    def _verified_block(self, index: int) -> bytes:
        self._source.seek(index * BLOCK_SIZE)
        expected = min(BLOCK_SIZE, self._size - index * BLOCK_SIZE)
        block = self._source.read(expected)
        if len(block) != expected or hashlib.sha256(block).hexdigest() != self._hashes[index]:
            raise DicomSourceChangedError()
        return block

    def verify_read_blocks(self) -> None:
        self._checkClosed()
        for index in sorted(self._read_blocks):
            self._verified_block(index)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError("Invalid seek origin.")
        base = 0 if whence == io.SEEK_SET else self._position if whence == io.SEEK_CUR else self._size
        if base + offset < 0:
            raise ValueError("Negative seek position.")
        self._position = base + offset
        return self._position

    def readinto(self, buffer) -> int:
        self._checkClosed()
        view = memoryview(buffer).cast("B")
        length = min(len(view), max(0, self._size - self._position))
        copied = 0
        while copied < length:
            index, offset = divmod(self._position, BLOCK_SIZE)
            if index != self._block_index:
                self._block_index, self._block = index, self._verified_block(index)
                self._read_blocks.add(index)
            amount = min(length - copied, len(self._block) - offset)
            view[copied:copied + amount] = self._block[offset:offset + amount]
            copied += amount
            self._position += amount
        return copied

    def close(self) -> None:
        self._block = b""
        super().close()
