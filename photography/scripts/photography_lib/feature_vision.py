"""Reusable isolated vision worker; only binary input and validated metadata cross IPC."""
from __future__ import annotations

import copy
import json
import os
import queue
import struct
import subprocess
import threading

from .config import PhotographyError
from . import feature_models
from .feature_profiles import validate_payload

_MAX_HEADER = 1024 * 1024
_MAX_IMAGE = 256 * 1024 * 1024
_MAX_RESPONSE = 64 * 1024 * 1024
_TIMEOUT_SECONDS = 180


class VisionProvider:
    def __init__(self, profile, *, config, python_path=None):
        feature_models.validate_provider_profile(profile)
        self._profile = copy.deepcopy(profile)
        self.config, self.python_path = config, python_path
        self._process = None
        self._closed = False
        self._timeout = _TIMEOUT_SECONDS
        self._mutex = threading.Lock()
        self.last_metadata = None

    def profile(self):
        return copy.deepcopy(self._profile)

    def check_ready(self):
        return feature_models.check_component(self._profile, config=self.config, python_path=self.python_path)

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *_):
        self.close()

    def _start(self):
        if self._closed:
            raise PhotographyError("FEATURE_WORKER_CLOSED", "Feature provider has been closed.")
        if self._process is not None:
            if self._process.poll() is not None:
                raise PhotographyError("FEATURE_WORKER_CRASHED", "Feature worker exited unexpectedly.")
            return
        self.check_ready()
        environment = os.environ.copy()
        environment.update({"OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"})
        try:
            self._process = subprocess.Popen(
                feature_models.worker_command(self.python_path), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, bufsize=0,
            )
        except OSError as exc:
            raise PhotographyError("FEATURE_RUNTIME_MISSING", "Cannot start the feature worker.") from exc
        try:
            response = self._exchange({"op": "init", "profile": self._profile,
                                       "paths": feature_models.asset_paths(self._profile, config=self.config)})
            if response != {"ok": True, "ready": True, "model_calls": 0}:
                raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Unexpected worker initialization response.")
        except Exception:
            self.close()
            raise

    def _exchange(self, header, image=b""):
        header = dict(header, image_size=len(image))
        encoded = json.dumps(header, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_HEADER:
            raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Worker request metadata is too large.")
        replies = queue.Queue(maxsize=1)
        process = self._process

        def transact():
            try:
                data = memoryview(struct.pack(">I", len(encoded)) + encoded)
                while data:
                    written = process.stdin.write(data)
                    if not written:
                        raise EOFError
                    data = data[written:]
                data = memoryview(image)
                while data:
                    written = process.stdin.write(data)
                    if not written:
                        raise EOFError
                    data = data[written:]
                process.stdin.flush()
                prefix = _read_exact(process.stdout, 4)
                size = struct.unpack(">I", prefix)[0]
                if not 0 < size <= _MAX_RESPONSE:
                    raise ValueError("Invalid response frame size.")
                replies.put((True, json.loads(_read_exact(process.stdout, size))))
            except (OSError, EOFError, ValueError, UnicodeError, RecursionError, struct.error) as exc:
                replies.put((False, exc))

        thread = threading.Thread(target=transact, daemon=True)
        thread.start()
        try:
            ok, response = replies.get(timeout=self._timeout)
        except queue.Empty as exc:
            self.close()
            thread.join(timeout=2)
            raise PhotographyError("FEATURE_WORKER_TIMEOUT", "Feature worker exceeded the operation deadline.") from exc
        thread.join(timeout=2)
        if not ok:
            self.close()
            code = "FEATURE_WORKER_CRASHED" if isinstance(response, (OSError, EOFError)) else "FEATURE_WORKER_PROTOCOL"
            raise PhotographyError(code, "Feature worker returned an incomplete or invalid response.") from response
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            self.close()
            raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Feature worker response is not a status object.")
        if not response["ok"]:
            error = response.get("error")
            self.close()
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Malformed worker error.")
            raise PhotographyError(error["code"], error["message"])
        return response

    def compute(self, image_bytes):
        if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > _MAX_IMAGE:
            raise PhotographyError("FEATURE_IMAGE_INVALID", "Provide nonempty local image bytes within the 256 MiB limit.")
        with self._mutex:
            self._start()
            response = self._exchange({"op": "compute"}, image_bytes)
            try:
                if set(response) != {"ok", "payload", "metadata"}:
                    raise ValueError("Unexpected worker output fields.")
                payload = validate_payload(self._profile, response["payload"])
                metadata = response["metadata"]
                if (not isinstance(metadata, dict)
                        or set(metadata) != {"total_count", "returned_count", "truncated", "onnx_calls"}
                        or any(type(metadata[k]) is not int or metadata[k] < 0
                               for k in ("total_count", "returned_count", "onnx_calls"))
                        or type(metadata["truncated"]) is not bool):
                    raise ValueError("Invalid worker count metadata.")
                count = len(payload["blocks" if self._profile["component"] == "ocr" else "objects"])
                if (metadata["returned_count"] != count or metadata["total_count"] < count
                        or metadata["truncated"] != (metadata["total_count"] > count)
                        or metadata["truncated"] == payload["complete"] or metadata["onnx_calls"] < 1):
                    raise ValueError("Inconsistent worker completeness metadata.")
                self.last_metadata = copy.deepcopy(metadata)
                return payload
            except (ValueError, KeyError, TypeError, PhotographyError) as exc:
                self.close()
                raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Feature worker produced invalid typed metadata.") from exc

    def close(self):
        process, self._process = self._process, None
        self._closed = True
        if process is None:
            return
        # The worker never creates child processes. Terminate only our own Popen PID.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()


def _read_exact(stream, length):
    result = bytearray()
    while len(result) < length:
        chunk = stream.read(length - len(result))
        if not chunk:
            raise EOFError
        result.extend(chunk)
    return bytes(result)
