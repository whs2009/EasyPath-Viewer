from __future__ import annotations

import os

from pathlib import Path

def _windows_path(path: str | Path, *, force_extended: bool = False) -> str:
    filename = os.path.abspath(os.fspath(path))
    if "\0" in filename:
        raise ValueError("Checkpoint paths cannot contain a null character.")
    if (force_extended or len(filename) >= 248) and not filename.startswith("\\\\?\\"):
        filename = "\\\\?\\UNC\\" + filename[2:] if filename.startswith("\\\\") else "\\\\?\\" + filename
    return filename

def native_file_path(path: str | Path, *, force_extended: bool = False) -> Path:
    """Adapt an I/O operand, never a serialized path or artifact identity."""
    return Path(_windows_path(path, force_extended=force_extended)) if os.name == "nt" else Path(path)
