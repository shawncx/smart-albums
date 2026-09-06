"""Explicit installation and offline CPU-only SigLIP2 image/text encoding."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import sysconfig
import time
import urllib.error
import urllib.request
import uuid

from . import index_profiles
from .fingerprints import fingerprint
from .config import PhotographyError
from .image_vectors import pack_vector, unpack_vector, validate_vector

_CHUNK_SIZE = 1024 * 1024
_GENERATION_PATTERN = r"(?:[0-9a-f]{64}-)?[0-9a-f]{32}"
_INSTALL_HELP = (
    "Use a dedicated 64-bit CPython 3.14 environment: "
    "py -3.14 -m venv .venv-index; "
    ".venv-index\\Scripts\\python -m pip install -r photography\\requirements-index.txt"
)


@dataclass(frozen=True)
class Encoding:
    vector: list[float]
    elapsed_seconds: float
    token_count: int | None = None


def _check_runtime() -> dict:
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14)
            or struct.calcsize("P") != 8 or sysconfig.get_config_var("Py_GIL_DISABLED")
            or platform.machine().lower() not in {"amd64", "x86_64"}):
        raise PhotographyError("INDEX_DEPENDENCY_MISSING", _INSTALL_HELP)
    versions = {}
    for package, expected in index_profiles.RUNTIME_PACKAGES.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise PhotographyError(
                "INDEX_DEPENDENCY_MISSING", f"Missing {package}=={expected}. {_INSTALL_HELP}"
            ) from exc
        if actual != expected:
            raise PhotographyError(
                "INDEX_MODEL_INVALID",
                f"Runtime identity mismatch: {package}=={expected} required, found {actual}. {_INSTALL_HELP}",
            )
        versions[package] = actual
    return versions


def _file_valid(path: Path, expected: dict) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"]:
            return False
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == expected["sha256"]
    except OSError as exc:
        raise PhotographyError("INDEX_MODEL_INVALID", f"Cannot verify model file: {path.name}.") from exc


def _snapshot_dir(model_dir: Path) -> Path:
    pointer = model_dir / "CURRENT"
    if not pointer.exists():
        return model_dir
    try:
        name = pointer.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise PhotographyError("INDEX_MODEL_INVALID", "Model snapshot pointer is unreadable.") from exc
    if not re.fullmatch(_GENERATION_PATTERN, name):
        raise PhotographyError("INDEX_MODEL_INVALID", "Invalid model snapshot pointer.")
    snapshot = model_dir / ".snapshots" / name
    if snapshot.is_symlink():
        raise PhotographyError("INDEX_MODEL_INVALID", "Model snapshots cannot be symbolic links.")
    return snapshot


def _validate_files(directory: Path):
    missing, invalid = [], []
    for filename, expected in index_profiles.FILES.items():
        path = directory / filename
        if not path.exists():
            missing.append(filename)
        elif not _file_valid(path, expected):
            invalid.append(filename)
    if invalid:
        raise PhotographyError(
            "INDEX_MODEL_INVALID", "Model files failed their fixed size/SHA-256 checks.",
            details={"files": invalid},
        )
    if missing:
        raise PhotographyError(
            "INDEX_MODEL_MISSING", "Local model is incomplete; run index setup explicitly.",
            details={"files": missing},
        )


@contextmanager
def _setup_lock(model_dir: Path):
    handle = (model_dir / ".setup.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise PhotographyError("INDEX_SETUP_BUSY", "Another model setup is active.") from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _download_file(filename: str, destination: Path, expected: dict):
    """Resume a partial fixed-revision file; publish it only after verification."""
    partial = destination.with_name(destination.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset >= expected["size"]:
        if _file_valid(partial, expected):
            partial.replace(destination)
            return
        partial.unlink()
        offset = 0
    url = f"https://huggingface.co/{index_profiles.REPO}/resolve/{index_profiles.REVISION}/{filename}"
    headers = {"Accept-Encoding": "identity", "User-Agent": "smart-albums-index-setup/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status == 206:
                expected_range = f"bytes {offset}-{expected['size'] - 1}/{expected['size']}"
                if response.headers.get("Content-Range") != expected_range:
                    raise PhotographyError("INDEX_MODEL_INVALID", f"Invalid download range for {filename}.")
            elif response.status == 200:
                offset = 0
            else:
                raise PhotographyError("INDEX_DOWNLOAD_FAILED", f"Unexpected download status for {filename}.")
            written = offset
            with partial.open("ab" if offset else "wb") as handle:
                while chunk := response.read(_CHUNK_SIZE):
                    written += len(chunk)
                    if written > expected["size"]:
                        raise PhotographyError("INDEX_MODEL_INVALID", f"Download exceeds fixed size: {filename}.")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise PhotographyError(
            "INDEX_DOWNLOAD_FAILED", f"Download interrupted for {filename}; rerun setup to resume."
        ) from exc
    if not _file_valid(partial, expected):
        # A complete but incorrect file must not poison all subsequent retries.
        if partial.stat().st_size == expected["size"]:
            partial.unlink()
        raise PhotographyError("INDEX_MODEL_INVALID", f"Download failed size/SHA-256 check: {filename}.")
    partial.replace(destination)


def _publish_current(root: Path, generation: str):
    new_pointer = root / "CURRENT.new"
    with new_pointer.open("w", encoding="ascii") as handle:
        handle.write(generation + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    new_pointer.replace(root / "CURRENT")


def setup_model(model_dir) -> dict:
    """The sole network path. Staging is resumable; CURRENT changes atomically."""
    _check_runtime()
    root = Path(model_dir).expanduser().resolve()
    profile = index_profiles.default_profile()
    profile_id = fingerprint(profile)
    downloaded, reused = [], []
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _setup_lock(root):
            invalid_pointer = False
            try:
                current = _snapshot_dir(root)
            except PhotographyError as exc:
                if exc.code != "INDEX_MODEL_INVALID":
                    raise
                current = root
                invalid_pointer = True
            valid_current = {
                name for name, expected in index_profiles.FILES.items()
                if _file_valid(current / name, expected)
            }
            if len(valid_current) == len(index_profiles.FILES) and not invalid_pointer:
                reused = sorted(valid_current)
            else:
                snapshots = root / ".snapshots"
                snapshots.mkdir(exist_ok=True)
                # A crash between the directory rename and CURRENT publication
                # leaves a fully verified generation that can still be reused.
                ready = next((
                    candidate for candidate in sorted(snapshots.iterdir())
                    if candidate.is_dir() and not candidate.is_symlink()
                    and re.fullmatch(_GENERATION_PATTERN, candidate.name)
                    and all(_file_valid(candidate / name, expected)
                            for name, expected in index_profiles.FILES.items())
                ), None)
                if ready is not None:
                    reused = list(index_profiles.FILES)
                    generation = ready.name
                else:
                    stage = root / ".staging" / profile_id
                    stage.mkdir(parents=True, exist_ok=True)
                    for name, expected in index_profiles.FILES.items():
                        destination = stage / name
                        if _file_valid(destination, expected):
                            reused.append(name)
                        elif name in valid_current:
                            shutil.copyfile(current / name, destination)
                            reused.append(name)
                        else:
                            _download_file(name, destination, expected)
                            downloaded.append(name)
                    _validate_files(stage)
                    # The full profile is already in the manifest; repeating its
                    # hash here can make otherwise valid Windows paths unreadable.
                    generation = uuid.uuid4().hex
                    stage.replace(snapshots / generation)
                _validate_files(snapshots / generation)
                _publish_current(root, generation)
    except OSError as exc:
        raise PhotographyError("INDEX_MODEL_INVALID", f"Cannot publish model installation: {exc}") from exc
    return {
        "status": "ready",
        "model_dir": str(root),
        "profile": profile,
        "profile_id": profile_id,
        "downloaded_files": downloaded,
        "reused_files": reused,
        "total_bytes": sum(item["size"] for item in index_profiles.FILES.values()),
        "downloaded_bytes": sum(index_profiles.FILES[name]["size"] for name in downloaded),
    }


def _load_runtime():
    try:
        import torch
        from transformers import GemmaTokenizerFast, SiglipImageProcessor, SiglipModel
    except (ImportError, OSError) as exc:
        raise PhotographyError("INDEX_DEPENDENCY_MISSING", f"Cannot import index runtime. {_INSTALL_HELP}") from exc
    return torch, SiglipModel, SiglipImageProcessor, GemmaTokenizerFast


def _decode_jpeg(data: bytes):
    try:
        from PIL import Image
        from .thumbnails import validate_preview
    except ImportError as exc:
        raise PhotographyError("INDEX_DEPENDENCY_MISSING", f"JPEG decoding requires Pillow. {_INSTALL_HELP}") from exc
    validate_preview(data)
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


class SiglipEncoder:
    def __init__(self, model_dir, profile=None):
        expected = index_profiles.default_profile()
        try:
            supported = profile is None or fingerprint(profile) == fingerprint(expected)
        except (TypeError, ValueError) as exc:
            raise PhotographyError("INDEX_MODEL_INVALID", "Unsupported index profile.") from exc
        if not supported:
            raise PhotographyError("INDEX_MODEL_INVALID", "Only the exact built-in SigLIP2 profile is supported.")
        self.model_dir = Path(model_dir).expanduser().resolve()
        self._profile = expected
        self._model = self._processor = self._tokenizer = self._torch = None
        self.calls = 0
        self.load_seconds = 0.0

    def profile(self) -> dict:
        return index_profiles._freeze(self._profile)

    def check_ready(self) -> dict:
        """Full file hashes and installed package metadata, never model loading."""
        directory = _snapshot_dir(self.model_dir)
        _validate_files(directory)
        runtime = _check_runtime()
        return {
            "status": "ready", "model_dir": str(self.model_dir),
            "profile_id": fingerprint(self._profile), "runtime": runtime,
        }

    def _ensure_loaded(self):
        if self._model is not None:
            return
        self.check_ready()
        directory = _snapshot_dir(self.model_dir)
        started = time.perf_counter()
        try:
            torch, model_class, processor_class, tokenizer_class = _load_runtime()
            torch.set_num_threads(self._profile["runtime"]["max_cpu_threads"])
            if torch.get_num_interop_threads() != 1:
                torch.set_num_interop_threads(1)
            if torch.version.cuda is not None:
                raise PhotographyError("INDEX_MODEL_INVALID", "The fixed runtime requires a CPU-only PyTorch build.")
            local = {"local_files_only": True, "trust_remote_code": False}
            processor = processor_class.from_pretrained(str(directory), **local)
            tokenizer = tokenizer_class.from_pretrained(str(directory), **local)
            model = model_class.from_pretrained(
                str(directory), **local, use_safetensors=True,
                torch_dtype=torch.float32, attn_implementation="eager",
            )
            text = self._profile["text"]
            if (model.config.text_config.max_position_embeddings != text["max_length"]
                    or model.config.text_config.hidden_size != self._profile["dimensions"]
                    or model.config.vision_config.hidden_size != self._profile["dimensions"]
                    or model.config.vision_config.image_size != 224
                    or tokenizer.add_bos_token is not False
                    or tokenizer.add_eos_token is not True
                    or tokenizer.eos_token_id != text["eos_token_id"]
                    or tokenizer.bos_token_id != text["bos_token_id"]
                    or tokenizer.pad_token_id != text["pad_token_id"]
                    or tokenizer.padding_side != text["padding_side"]):
                raise PhotographyError("INDEX_MODEL_INVALID", "Loaded model/tokenizer does not match the fixed profile.")
            model.to(device="cpu", dtype=torch.float32)
            model.eval()
            model.requires_grad_(False)
            self._torch, self._processor, self._tokenizer, self._model = torch, processor, tokenizer, model
        except (OSError, ValueError, TypeError, RuntimeError, AttributeError, MemoryError) as exc:
            raise PhotographyError("INDEX_MODEL_INVALID", f"Cannot load the fixed local CPU model: {exc}") from exc
        finally:
            self.load_seconds = time.perf_counter() - started

    def _vector(self, features) -> list[float]:
        dimensions = self._profile["dimensions"]
        features = features.detach().to(device="cpu", dtype=self._torch.float32)
        if tuple(features.shape) != (1, dimensions):
            raise PhotographyError("INDEX_ENCODING_FAILED", f"Expected one {dimensions}-dimension feature vector.")
        raw = validate_vector(features[0].tolist(), dimensions, normalized=False)
        norm = math.hypot(*raw)
        # Round through the storage representation; returned values exactly match
        # the little-endian float32 values subsequently persisted in SQLite.
        return unpack_vector(pack_vector([value / norm for value in raw], dimensions), dimensions)

    def encode_image(self, jpeg: bytes) -> Encoding:
        self.calls += 1
        started = time.perf_counter()
        try:
            with _decode_jpeg(jpeg) as image:
                loading_started = time.perf_counter()
                self._ensure_loaded()
                started += time.perf_counter() - loading_started
                inputs = self._processor(images=image, return_tensors="pt")
            pixels = inputs["pixel_values"].to(device="cpu", dtype=self._torch.float32)
            with self._torch.inference_mode():
                vector = self._vector(self._model.get_image_features(pixel_values=pixels))
            return Encoding(vector, time.perf_counter() - started)
        except (OSError, ValueError, TypeError, RuntimeError, AttributeError, KeyError, MemoryError) as exc:
            raise PhotographyError("INDEX_ENCODING_FAILED", f"Image encoding failed: {exc}") from exc

    def encode_text(self, text: str) -> Encoding:
        self.calls += 1
        if not isinstance(text, str) or not text.strip():
            raise PhotographyError("INVALID_QUERY", "A nonblank text query is required.")
        self._ensure_loaded()
        started = time.perf_counter()
        try:
            policy = self._profile["text"]
            counted = self._tokenizer(
                text, add_special_tokens=True, padding=False, truncation=False,
                return_attention_mask=False, return_token_type_ids=False,
            )
            token_count = len(counted["input_ids"])
            if token_count > policy["max_length"]:
                raise PhotographyError(
                    "QUERY_TOO_LONG",
                    f"Query uses {token_count} tokens including EOS; maximum is {policy['max_length']}.",
                    details={"token_count": token_count, "max_tokens": policy["max_length"]},
                )
            inputs = self._tokenizer(
                text, add_special_tokens=True, padding="max_length",
                max_length=policy["max_length"], truncation=False, return_tensors="pt",
                return_attention_mask=False, return_token_type_ids=False,
            )
            ids = inputs["input_ids"].to(device="cpu")
            if tuple(ids.shape) != (1, policy["max_length"]):
                raise PhotographyError("INDEX_ENCODING_FAILED", "Tokenizer did not produce the fixed padded context.")
            with self._torch.inference_mode():
                vector = self._vector(self._model.get_text_features(input_ids=ids))
            return Encoding(vector, time.perf_counter() - started, token_count)
        except (OSError, ValueError, TypeError, RuntimeError, AttributeError, KeyError, MemoryError) as exc:
            raise PhotographyError("INDEX_ENCODING_FAILED", f"Text encoding failed: {exc}") from exc
