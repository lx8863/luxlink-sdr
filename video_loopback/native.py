"""Put the matching libiio v0.25 DLL ahead of an older IIO-Scope install."""

from __future__ import annotations

import os
from pathlib import Path


_dll_handle = None


def prepare_libiio() -> None:
    global _dll_handle
    if os.name != "nt":
        return
    supplied = os.environ.get("IIO_LIB_DIR")
    directory = Path(supplied) if supplied else Path(__file__).resolve().parents[1] / "Windows-VS-2022-x64"
    dll = directory / "libiio.dll"
    if not dll.is_file():
        raise RuntimeError(
            f"libiio 0.25 DLL was not found at {dll}. "
            "Restore the bundled Windows-VS-2022-x64 directory or set IIO_LIB_DIR."
        )
    os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    _dll_handle = os.add_dll_directory(str(directory))
