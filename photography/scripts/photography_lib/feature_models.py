"""Explicit, checksummed asset preparation for isolated CPU vision workers."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.request
import uuid

from .config import PhotographyError
from .feature_profiles import COCO_CLASS_IDS


RUNTIME_PACKAGES = {
    "rapidocr": "3.9.2", "onnxruntime": "1.29.0", "numpy": "2.4.2",
    "Pillow": "12.3.0", "opencv-python": "4.13.0.92", "pyclipper": "1.4.0",
    "Shapely": "2.1.2", "PyYAML": "6.0.3", "omegaconf": "2.3.1",
    "antlr4-python3-runtime": "4.9.3", "six": "1.17.0", "tqdm": "4.70.0",
    "requests": "2.34.2", "colorlog": "6.12.0", "colorama": "0.4.6",
    "flatbuffers": "25.12.19", "packaging": "26.3", "protobuf": "7.36.0",
    "charset-normalizer": "3.5.1", "idna": "3.19", "urllib3": "2.7.0",
    "certifi": "2026.7.22",
}
_RAPID_ROOT = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx"
_YOLOX_URL = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"
)
YOLOX_LICENSE_NOTICE = (
    "YOLOX source code is Apache-2.0, but the official release does not explicitly "
    "license its weights separately. Upstream issue #1865 is unanswered. Automated "
    "weight distribution and ordinary use remain gated pending owner review. "
    "SMART_ALBUMS_YOLOX_LICENSE_REVIEW=synthetic-evaluation permits only explicitly "
    "authorized synthetic evaluation; =approved records an operator's completed review."
)
ASSETS = {
    "ocr": [
        {"name": "det.onnx", "role": "text_detection", "size": 9929594,
         "sha256": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
         "url": _RAPID_ROOT + "/PP-OCRv6/det/PP-OCRv6_det_small.onnx",
         "revision": "RapidOCR-v3.9.2", "license": "Apache-2.0"},
        {"name": "rec.onnx", "role": "text_recognition_with_embedded_dictionary", "size": 21234383,
         "sha256": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
         "url": _RAPID_ROOT + "/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
         "revision": "RapidOCR-v3.9.2", "license": "Apache-2.0"},
        {"name": "cls.onnx", "role": "text_line_orientation", "size": 585532,
         "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
         "url": _RAPID_ROOT + "/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
         "revision": "RapidOCR-v3.9.2", "license": "Apache-2.0"},
    ],
    "objects": [
        {"name": "nano.onnx", "role": "object_detection", "size": 3659407,
         "sha256": "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
         "url": _YOLOX_URL, "revision": "YOLOX-0.1.1rc0-asset-42724905",
         "license": "weight-license-review-required"},
    ],
}
COCO_LABELS = COCO_CLASS_IDS
LICENSE_SOURCES = {
    "ocr": ["https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/README.md"],
    "objects": [
        "https://github.com/Megvii-BaseDetection/YOLOX/blob/6ddff4824372906469a7fae2dc3206c7aa4bbaee/LICENSE",
        "https://github.com/Megvii-BaseDetection/YOLOX/issues/1865",
    ],
}


def worker_python(python_path=None) -> Path:
    explicit = python_path if python_path is not None else os.environ.get("SMART_ALBUMS_FEATURE_PYTHON")
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise PhotographyError("FEATURE_RUNTIME_MISSING", "Feature Python must be an existing absolute executable path.")
        return path.resolve()
    project = Path(__file__).resolve().parents[3]
    candidate = project / ".venv-features" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if candidate.is_file():
        return candidate.resolve()
    raise PhotographyError(
        "FEATURE_RUNTIME_MISSING",
        "Prepare isolated .venv-features from CPython 3.14 and install photography/requirements-features.txt; "
        "or set SMART_ALBUMS_FEATURE_PYTHON to an existing absolute interpreter path.",
    )


def runtime_identity() -> dict:
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14)
            or struct.calcsize("P") != 8 or sysconfig.get_config_var("Py_GIL_DISABLED")
            or platform.machine().lower() not in {"amd64", "x86_64"}):
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "Use standard 64-bit x86 CPython 3.14.")
    versions = {}
    for name, expected in RUNTIME_PACKAGES.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise PhotographyError("FEATURE_RUNTIME_MISSING", f"Missing {name}=={expected} in isolated worker.") from exc
        if actual != expected:
            raise PhotographyError("FEATURE_RUNTIME_INVALID", f"Expected {name}=={expected}; found {actual}.")
        versions[name] = actual
    try:
        import cv2
        import onnxruntime
        import rapidocr
    except (ImportError, OSError) as exc:
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "A pinned native vision dependency cannot be imported.") from exc
    onnxruntime.disable_telemetry_events()
    if "CPUExecutionProvider" not in onnxruntime.get_available_providers():
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "ONNX Runtime has no CPU execution provider.")
    return {
        "implementation": "cpython", "python": platform.python_version(),
        "platform": sys.platform, "architecture": platform.machine().lower(), "packages": versions,
        "providers": ["CPUExecutionProvider"], "intra_op_num_threads": 2, "inter_op_num_threads": 1,
        "backend": "onnxruntime", "device": "cpu",
    }


def worker_command(python_path=None) -> list[str]:
    # -I ignores PYTHONPATH, user-site packages, and Python startup environment hooks.
    script = Path(__file__).with_name("feature_worker.py")
    return [str(worker_python(python_path)), "-I", str(script)]


def probe_runtime(python_path=None) -> dict:
    try:
        result = subprocess.run(
            worker_command(python_path) + ["--runtime"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PhotographyError("FEATURE_WORKER_TIMEOUT", "Feature runtime check timed out.") from exc
    except OSError as exc:
        raise PhotographyError("FEATURE_RUNTIME_MISSING", "Cannot launch the isolated feature interpreter.") from exc
    if result.returncode:
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "Isolated runtime check failed; verify pinned requirements.")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Invalid runtime check response.") from exc
    if not isinstance(value, dict) or value.get("packages") != RUNTIME_PACKAGES:
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "Worker runtime does not match pinned dependencies.")
    return value


def _license_gate(component):
    if component == "objects" and os.environ.get("SMART_ALBUMS_YOLOX_LICENSE_REVIEW") not in {
        "approved", "synthetic-evaluation",
    }:
        raise PhotographyError("FEATURE_LICENSE_REVIEW_REQUIRED", YOLOX_LICENSE_NOTICE)


def _asset_identity(component: str) -> str:
    return hashlib.sha256(json.dumps(ASSETS[component], sort_keys=True).encode()).hexdigest()[:16]


def component_directory(component, *, config) -> Path:
    if component not in ASSETS:
        raise PhotographyError("FEATURE_COMPONENT_INVALID", "This provider supports only ocr and objects.")
    path = Path(config.model_cache_root) / "features-v1" / component / _asset_identity(component)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PhotographyError("FEATURE_MODEL_INVALID", "Feature asset cache cannot traverse symbolic links.")
    return path


def _valid_file(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"]


def _verify_directory(directory, component):
    if directory.is_symlink():
        raise PhotographyError("FEATURE_MODEL_INVALID", "Feature asset directories must not be symbolic links.")
    try:
        if (directory / "OWNER.json").is_symlink():
            raise PhotographyError("FEATURE_MODEL_INVALID", "Feature cache ownership marker cannot be a symbolic link.")
        marker = json.loads((directory / "OWNER.json").read_text(encoding="utf-8"))
        if marker != {"owner": "smart-albums-feature-assets-v1", "assets": ASSETS[component]}:
            raise PhotographyError("FEATURE_MODEL_INVALID", "Feature cache ownership or manifest mismatch.")
        for asset in ASSETS[component]:
            if not _valid_file(directory / asset["name"], asset):
                raise PhotographyError("FEATURE_MODEL_INVALID", f"Feature asset checksum failed: {asset['name']}.")
    except FileNotFoundError as exc:
        raise PhotographyError("FEATURE_MODEL_MISSING", "Run explicit feature setup to prepare the complete local model.") from exc
    except (OSError, ValueError, UnicodeError) as exc:
        raise PhotographyError("FEATURE_MODEL_INVALID", "Cannot verify feature asset manifest.") from exc


def asset_paths(profile, *, config) -> dict:
    component = profile.get("component")
    directory = component_directory(component, config=config)
    _verify_directory(directory, component)
    return {item["name"]: str(directory / item["name"]) for item in ASSETS[component]}


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.split(":", 1)[0].lower() != "https":
            raise PhotographyError("FEATURE_DOWNLOAD_FAILED", "Model redirects must retain HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(asset, path):
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "smart-albums-features/1", "Accept-Encoding": "identity"})
    try:
        opener = urllib.request.build_opener(_HTTPSRedirectHandler())
        with opener.open(request, timeout=60) as response, path.open("xb") as stream:
            if response.geturl().split(":", 1)[0] != "https":
                raise PhotographyError("FEATURE_DOWNLOAD_FAILED", "Model download redirected outside HTTPS.")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > asset["size"]:
                    raise PhotographyError("FEATURE_MODEL_INVALID", "Downloaded model exceeds its pinned size.")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise PhotographyError("FEATURE_DOWNLOAD_FAILED", f"Cannot download verified asset {asset['name']}.") from exc
    if not _valid_file(path, asset):
        raise PhotographyError("FEATURE_MODEL_INVALID", f"Asset size/SHA-256 mismatch: {asset['name']}.")


@contextmanager
def _setup_lock(parent):
    from .exports import publish_new_file

    lock = parent / ".setup.lock"
    marker = b"\0smart-albums-feature-setup-lock-v1\n"
    if lock.is_symlink():
        raise PhotographyError("FEATURE_SETUP_BUSY", "Feature setup cannot use a symbolic-link lock.")
    if not lock.exists():
        stage = parent / (".setup-lock-" + uuid.uuid4().hex + ".tmp")
        marker_stream = stage.open("xb")
        try:
            with marker_stream:
                marker_stream.write(marker)
                marker_stream.flush()
                os.fsync(marker_stream.fileno())
            try:
                # A complete marker is published without replacing a competing lock.
                publish_new_file(stage, lock)
            except FileExistsError:
                pass
        finally:
            stage.unlink(missing_ok=True)
    if lock.is_symlink():
        raise PhotographyError("FEATURE_SETUP_BUSY", "Feature setup cannot use a symbolic-link lock.")
    try:
        stream = lock.open("r+b")
    except OSError as exc:
        raise PhotographyError("FEATURE_SETUP_BUSY", "Cannot safely open the existing feature setup lock.") from exc
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise PhotographyError("FEATURE_SETUP_BUSY", "Another feature model setup is active.") from exc
        if stream.read(len(marker) + 1) != marker:
            raise PhotographyError("FEATURE_SETUP_BUSY", "An unknown or legacy feature setup lock must not be overwritten.")
        yield
    finally:
        try:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            # Keep the same inode so waiters and subsequent processes lock one file.
            stream.close()


def prepared_profile(component, runtime):
    from .feature_profiles import default_profile
    profile = copy.deepcopy(default_profile(component))
    profile["assets"] = _profile_assets(component)
    profile["runtime"] = copy.deepcopy(runtime)
    return profile


def _profile_assets(component):
    identity_fields = {"name", "role", "size", "sha256", "revision", "license"}
    return [{key: copy.deepcopy(value) for key, value in item.items() if key in identity_fields}
            for item in ASSETS[component]]


def validate_provider_profile(profile):
    from .feature_profiles import profile_identity
    profile_identity(profile)
    component = profile.get("component")
    if component not in ASSETS or profile.get("assets") != _profile_assets(component):
        raise PhotographyError("FEATURE_MODEL_INVALID", "Profile assets do not match this fixed provider.")
    expected = prepared_profile(component, profile["runtime"])
    if profile.get("provider") != expected["provider"] or profile.get("input_scope") != expected["input_scope"]:
        raise PhotographyError("FEATURE_MODEL_INVALID", "Unsupported vision provider or input scope.")
    fixed = expected["parameters"]
    configurable = ({"use_cls", "text_score", "det_limit_side_len", "det_thresh", "det_box_thresh",
                     "det_unclip_ratio", "max_blocks", "max_text_chars", "max_input_pixels"} if component == "ocr"
                    else {"score_threshold", "nms_threshold", "class_agnostic_nms", "max_detections"})
    if profile["parameters"].keys() != fixed.keys():
        raise PhotographyError("FEATURE_MODEL_INVALID", "Unexpected or missing vision recipe parameter.")
    for key, value in fixed.items():
        if key not in configurable and profile["parameters"][key] != value:
            raise PhotographyError("FEATURE_MODEL_INVALID", f"Unsupported vision parameter: {key}.")


def setup_component(component, *, config, python_path=None) -> dict:
    directory = component_directory(component, config=config)
    _license_gate(component)
    runtime = probe_runtime(python_path)
    downloaded = []
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        with _setup_lock(directory.parent):
            if directory.exists():
                _verify_directory(directory, component)
            else:
                stage = directory.parent / ("s-" + uuid.uuid4().hex[:12])
                stage.mkdir()
                try:
                    for asset in ASSETS[component]:
                        _download(asset, stage / asset["name"])
                        downloaded.append(asset["name"])
                    with (stage / "OWNER.json").open("x", encoding="utf-8") as stream:
                        json.dump({"owner": "smart-albums-feature-assets-v1", "assets": ASSETS[component]}, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    _verify_directory(stage, component)
                    if directory.exists():
                        _verify_directory(directory, component)
                    else:
                        stage.rename(directory)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
    except OSError as exc:
        raise PhotographyError("FEATURE_SETUP_FAILED", "Cannot prepare the feature asset cache without overwriting existing files.") from exc
    profile = prepared_profile(component, runtime)
    validate_provider_profile(profile)
    return {
        "profile": profile, "component": component, "ready": True, "downloaded": downloaded,
        "asset_directory": str(directory), "worker_python": str(worker_python(python_path)),
        "total_bytes": sum(item["size"] for item in ASSETS[component]),
        "sources": copy.deepcopy(ASSETS[component]),
        "license_sources": LICENSE_SOURCES[component],
        "license_review": ("not-required" if component == "ocr" else os.environ["SMART_ALBUMS_YOLOX_LICENSE_REVIEW"]),
        "license_notice": None if component == "ocr" else YOLOX_LICENSE_NOTICE,
    }


def check_component(profile, *, config, python_path=None) -> dict:
    validate_provider_profile(profile)
    _license_gate(profile["component"])
    paths = asset_paths(profile, config=config)
    actual = probe_runtime(python_path)
    if actual != profile["runtime"]:
        raise PhotographyError("FEATURE_RUNTIME_INVALID", "Feature runtime differs from the registered profile.")
    return {"ready": True, "component": profile["component"], "asset_count": len(paths),
            "runtime": actual, "model_calls": 0, "downloads": 0}
