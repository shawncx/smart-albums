"""Pinned local ONNX text encoder. Only prepare_model() can access the network."""
from __future__ import annotations

import hashlib
import math
import os
import struct
import time
import urllib.request
from dataclasses import dataclass
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from uuid import uuid4

from .analysis_schema import fingerprint
from .config import PhotographyError
from .embedding_text import RECIPE_VERSION

REPO = "Xenova/multilingual-e5-small"
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
WEIGHTS = "onnx/model_quantized.onnx"
FILES = {
    WEIGHTS: (118308185, "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
    "tokenizer.json": (17082730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}
QUERY_PREFIX = "query: "
DOCUMENT_PREFIX = "passage: "
RUNTIME = {"onnxruntime": "1.22.1", "tokenizers": "0.21.2", "numpy": "2.2.6"}


def default_profile():
    return {"model": "intfloat/multilingual-e5-small", "distribution": REPO, "revision": REVISION,
            "files": {key: value[1] for key, value in FILES.items()}, "runtime": dict(RUNTIME),
            "dimensions": 384, "dtype": "float32-le", "normalized": True, "pooling": "attention-mean",
            "weights_precision": "int8", "max_tokens": 512, "truncation": "right", "document_prefix": DOCUMENT_PREFIX,
            "query_prefix": QUERY_PREFIX, "recipe_version": RECIPE_VERSION}


def default_model_dir(state_dir):
    return Path(state_dir) / "models" / "multilingual-e5-small-onnx-int8"


def checked_file(path, expected):
    size, digest = expected
    try:
        if path.stat().st_size != size:
            return False
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == digest
    except OSError:
        return False


def prepare_model(model_dir):
    """Explicit setup; bounded downloads, pinned hashes, atomic per-file publication."""
    root = Path(model_dir).resolve()
    downloaded = reused = 0
    for name, expected in FILES.items():
        target = root / name
        if checked_file(target, expected):
            reused += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + "." + uuid4().hex + ".part")
        try:
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
            with urllib.request.urlopen(url, timeout=60) as response, temp.open("xb") as handle:
                count = 0
                while block := response.read(1024 * 1024):
                    count += len(block)
                    if count > expected[0]:
                        raise PhotographyError("MODEL_DOWNLOAD_FAILED", "Model download exceeds its pinned size.")
                    handle.write(block)
            if not checked_file(temp, expected):
                raise PhotographyError("MODEL_DOWNLOAD_FAILED", "Model checksum mismatch; existing files were preserved.")
            os.replace(temp, target)
            downloaded += 1
        except OSError as exc:
            raise PhotographyError("MODEL_DOWNLOAD_FAILED", "Could not download the local text model; rerun embedding-setup to resume.") from exc
        finally:
            temp.unlink(missing_ok=True)
    return {"status": "completed", "model_dir": str(root), "downloaded_files": downloaded,
            "reused_files": reused, "model_bytes": sum(v[0] for v in FILES.values()),
            "encoder_id": fingerprint(default_profile()), "visual_model_calls": 0}


def validate_vector(values, dimensions, *, normalized=True):
    try:
        values = [float(v) for v in values]
        if not 1 <= dimensions <= 4096 or len(values) != dimensions or not all(math.isfinite(v) for v in values):
            raise ValueError()
        norm = math.sqrt(math.fsum(v * v for v in values))
        if norm < 1e-12 or (normalized and abs(norm - 1) > .001):
            raise ValueError()
        return values
    except (ValueError, TypeError, OverflowError) as exc:
        raise PhotographyError("INVALID_VECTOR", "Vector must have the configured dimension and finite, normalized values.") from exc


def pack_vector(values, dimensions):
    return struct.pack(f"<{dimensions}f", *validate_vector(values, dimensions))


def unpack_vector(blob, dimensions):
    if not isinstance(blob, bytes) or not 1 <= dimensions <= 4096 or len(blob) != dimensions * 4:
        raise PhotographyError("INVALID_VECTOR", "Stored vector size does not match its encoding profile.")
    return validate_vector(struct.unpack(f"<{dimensions}f", blob), dimensions)


@dataclass
class Encoding:
    vector: list[float]
    token_count: int
    truncated: bool
    elapsed_seconds: float


class LocalEncoder:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self.session = self.tokenizer = None
        self.load_seconds = 0.0
        self.calls = 0

    def profile(self):
        return default_profile()

    def load(self):
        if self.session is not None:
            return
        started = time.perf_counter()
        if not all(checked_file(self.model_dir / name, expected) for name, expected in FILES.items()):
            raise PhotographyError("EMBEDDING_MODEL_MISSING", "Local model is missing or corrupt. Run embedding-setup; normal search never downloads files.")
        try:
            for package, expected in RUNTIME.items():
                if version(package) != expected:
                    raise PackageNotFoundError(package)
            import onnxruntime as ort
            from tokenizers import Tokenizer
            options = ort.SessionOptions()
            options.intra_op_num_threads = min(4, os.cpu_count() or 1)
            options.inter_op_num_threads = 1
            self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            self.tokenizer.no_padding()
            self.tokenizer.no_truncation()
            self.session = ort.InferenceSession(str(self.model_dir / WEIGHTS),
                sess_options=options, providers=["CPUExecutionProvider"])
        except (ImportError, PackageNotFoundError) as exc:
            raise PhotographyError("EMBEDDING_DEPENDENCY_MISSING", "Install this Skill's requirements-embedding.txt in the project environment.") from exc
        except Exception as exc:
            raise PhotographyError("EMBEDDING_MODEL_INVALID", "Local model could not be loaded by the pinned runtime.") from exc
        self.load_seconds = time.perf_counter() - started

    def encode(self, text, *, query=False):
        if not isinstance(text, str) or not text.strip():
            raise PhotographyError("INVALID_ARGUMENT", "Embedding text must not be empty.")
        self.load()
        import numpy as np
        started = time.perf_counter()
        content = (QUERY_PREFIX if query else DOCUMENT_PREFIX) + text
        full = self.tokenizer.encode(content)
        count = len(full.ids)
        if query and count > 512:
            raise PhotographyError("QUERY_TOO_LONG", "Search query exceeds 512 tokens including its retrieval prefix; shorten it.")
        # Explicitly retain the final SEP token when truncating document tails.
        ids = full.ids if count <= 512 else full.ids[:511] + [full.ids[-1]]
        types = full.type_ids[:len(ids)]
        inputs = {"input_ids": np.asarray([ids], dtype=np.int64),
                  "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
                  "token_type_ids": np.asarray([types], dtype=np.int64)}
        try:
            self.calls += 1
            outputs = self.session.run(None, {i.name: inputs[i.name] for i in self.session.get_inputs()})
            # Single unpadded sequence: all tokens have attention=1, so mean pooling
            # equals the model's documented attention-mask-weighted mean.
            vector = outputs[0][0].astype(np.float64).mean(axis=0)
            vector /= np.linalg.norm(vector)
            values = validate_vector(vector.tolist(), 384)
        except Exception as exc:
            raise PhotographyError("EMBEDDING_FAILED", "Local text inference failed or returned an invalid vector.") from exc
        return Encoding(values, count, count > 512, time.perf_counter() - started)
