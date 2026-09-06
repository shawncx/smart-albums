"""Strict, dependency-free storage boundary for normalized image vectors."""
from __future__ import annotations

import math
import numbers
import struct

from .config import PhotographyError

NORM_TOLERANCE = 1e-4


def validate_vector(values, dimensions, *, normalized=True) -> list[float]:
    try:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("Dimensions must be a positive integer.")
        items = list(values)
        if len(items) != dimensions:
            raise ValueError(f"Expected {dimensions} vector components, received {len(items)}.")
        if any(isinstance(item, bool) or not isinstance(item, numbers.Real) for item in items):
            raise ValueError("Vector components must be real numbers.")
        vector = [float(item) for item in items]
        if not all(math.isfinite(item) for item in vector):
            raise ValueError("Vector components must be finite.")
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("Vector must have a finite nonzero norm.")
        if normalized and abs(norm - 1.0) > NORM_TOLERANCE:
            raise ValueError("Vector must have unit L2 norm.")
        return vector
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhotographyError("INDEX_VECTOR_INVALID", str(exc)) from exc


def pack_vector(values, dimensions) -> bytes:
    vector = validate_vector(values, dimensions)
    try:
        blob = struct.pack(f"<{dimensions}f", *vector)
    except (struct.error, OverflowError) as exc:
        raise PhotographyError("INDEX_VECTOR_INVALID", "Vector is not representable as float32.") from exc
    unpack_vector(blob, dimensions)
    return blob


def unpack_vector(blob, dimensions) -> list[float]:
    if (isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0
            or not isinstance(blob, (bytes, bytearray, memoryview))
            or len(blob) != dimensions * 4):
        raise PhotographyError("INDEX_VECTOR_INVALID", "Vector BLOB must contain exactly dimensions * 4 bytes.")
    try:
        values = struct.unpack(f"<{dimensions}f", blob)
    except (struct.error, BufferError, TypeError) as exc:
        raise PhotographyError("INDEX_VECTOR_INVALID", "Invalid little-endian float32 vector BLOB.") from exc
    return validate_vector(values, dimensions)
