from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib.config import Config, PhotographyError
from photography_lib import feature_models as models
from photography_lib.feature_profiles import default_profile, profile_identity, validate_payload
from photography_lib.feature_vision import VisionProvider
from photography_lib import feature_worker as worker

HAS_VISION_RUNTIME = all(importlib.util.find_spec(name) for name in ("numpy", "cv2", "onnxruntime", "rapidocr"))
FAKE_RUNTIME = {
    "implementation": "cpython", "python": "3.14.7", "platform": "win32",
    "architecture": "amd64", "packages": models.RUNTIME_PACKAGES,
    "providers": ["CPUExecutionProvider"], "intra_op_num_threads": 2,
    "inter_op_num_threads": 1, "backend": "onnxruntime", "device": "cpu",
}


def ready_profile(component="objects"):
    return models.prepared_profile(component, FAKE_RUNTIME)


class FeatureModelTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / ".photography-state" / ("feature-unit-" + uuid.uuid4().hex[:12])
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.config = Config(self.root / "test.sqlite", model_cache_root=self.root / "models")

    def _fixtures(self):
        contents = b"synthetic model fixture"
        assets = {"ocr": [{"name": "fixture.onnx", "role": "text_detection", "size": len(contents),
                          "sha256": hashlib.sha256(contents).hexdigest(), "url": "https://example.invalid/fixed",
                          "revision": "fixed-v1", "license": "Apache-2.0"}]}
        return contents, assets

    def test_pins_and_fixed_official_assets(self):
        text = (PROJECT / "photography" / "requirements-features.txt").read_text()
        for name, version in models.RUNTIME_PACKAGES.items():
            self.assertIn(f"{name}=={version}", text)
        self.assertEqual(sum(a["size"] for a in models.ASSETS["ocr"]), 31749509)
        self.assertEqual(models.ASSETS["objects"][0]["size"], 3659407)
        self.assertEqual(models.ASSETS["objects"][0]["sha256"],
                         "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d")
        self.assertEqual(models.COCO_LABELS, tuple(default_profile("objects")["parameters"]["labels"]))

    def test_worker_python_explicit_absolute_only(self):
        self.assertEqual(models.worker_python(sys.executable), Path(sys.executable).resolve())
        for path in ("python", "missing-python.exe", str(self.root)):
            with self.subTest(path=path), self.assertRaises(PhotographyError):
                models.worker_python(path)
        with patch.dict(os.environ, {"SMART_ALBUMS_FEATURE_PYTHON": sys.executable}):
            self.assertEqual(models.worker_python(), Path(sys.executable).resolve())

    def test_identity_contains_runtime_assets_not_machine_paths(self):
        profile = ready_profile()
        models.validate_provider_profile(profile)
        first = VisionProvider(profile, config=self.config, python_path=sys.executable)
        other = VisionProvider(profile, config=Config(self.root / "other.sqlite", model_cache_root=self.root / "other"))
        self.assertEqual(profile_identity(first.profile()), profile_identity(other.profile()))
        detached = first.profile()
        detached["parameters"]["max_detections"] = 1
        self.assertNotEqual(detached, first.profile())
        self.assertNotIn(str(self.root), profile_identity(first.profile())[1])

    def test_asset_identity_whitelists_fields_and_excludes_setup_locations(self):
        before = ready_profile()
        assets = copy.deepcopy(models.ASSETS)
        assets["objects"][0].update({"url": "https://example.invalid/alternate",
                                     "cache_path": str(self.root), "downloaded_at": "machine-local"})
        with patch.object(models, "ASSETS", assets):
            after = ready_profile()
            models.validate_provider_profile(after)
        self.assertEqual(profile_identity(before), profile_identity(after))
        self.assertEqual(set(after["assets"][0]), {"name", "role", "size", "sha256", "revision", "license"})

    def test_unprepared_profile_and_tampered_assets_rejected(self):
        with self.assertRaises(PhotographyError):
            VisionProvider(default_profile("objects"), config=self.config)
        bad = ready_profile()
        bad["assets"][0]["sha256"] = "f" * 64
        with self.assertRaises(PhotographyError):
            models.validate_provider_profile(bad)

    def test_objects_license_gate_precedes_runtime_or_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(models, "probe_runtime") as probe:
            with self.assertRaises(PhotographyError) as caught:
                models.setup_component("objects", config=self.config)
            self.assertEqual(caught.exception.code, "FEATURE_LICENSE_REVIEW_REQUIRED")
            probe.assert_not_called()

    def test_setup_publishes_complete_verified_bundle_and_reuses(self):
        content, assets = self._fixtures()
        def download(asset, path):
            path.write_bytes(content)
        with patch.object(models, "ASSETS", assets), patch.object(models, "probe_runtime", return_value=FAKE_RUNTIME), \
                patch.object(models, "_download", side_effect=download) as fetch:
            result = models.setup_component("ocr", config=self.config, python_path=sys.executable)
            directory = Path(result["asset_directory"])
            self.assertTrue((directory / "OWNER.json").is_file())
            self.assertEqual(result["downloaded"], ["fixture.onnx"])
            again = models.setup_component("ocr", config=self.config, python_path=sys.executable)
            self.assertEqual(again["downloaded"], [])
            self.assertEqual(fetch.call_count, 1)
            before = sorted(self.root.rglob("*"))
            with patch.object(models, "_download", side_effect=AssertionError("Network forbidden")):
                checked = models.check_component(result["profile"], config=self.config, python_path=sys.executable)
            self.assertEqual(checked["model_calls"], 0)
            self.assertEqual(before, sorted(self.root.rglob("*")))

    def test_failed_setup_removes_only_own_staging_and_never_publishes(self):
        content, assets = self._fixtures()
        def corrupt_download(asset, path):
            path.write_bytes(content + b"bad")
        with patch.object(models, "ASSETS", assets), patch.object(models, "probe_runtime", return_value=FAKE_RUNTIME), \
                patch.object(models, "_download", side_effect=corrupt_download):
            directory = models.component_directory("ocr", config=self.config)
            with self.assertRaises(PhotographyError):
                models.setup_component("ocr", config=self.config, python_path=sys.executable)
            self.assertFalse(directory.exists())
            self.assertEqual(list(directory.parent.iterdir()), [])

    def test_existing_foreign_directory_is_never_overwritten(self):
        content, assets = self._fixtures()
        with patch.object(models, "ASSETS", assets), patch.object(models, "probe_runtime", return_value=FAKE_RUNTIME), \
                patch.object(models, "_download") as fetch:
            directory = models.component_directory("ocr", config=self.config)
            directory.mkdir(parents=True)
            (directory / "fixture.onnx").write_bytes(content)
            foreign = directory / "my-notes.txt"
            foreign.write_text("not owned", encoding="utf-8")
            with self.assertRaises(PhotographyError):
                models.setup_component("ocr", config=self.config, python_path=sys.executable)
            fetch.assert_not_called()
            self.assertEqual(foreign.read_text(), "not owned")
            self.assertFalse((directory / "OWNER.json").exists())

    def test_download_verifies_size_hash_and_https(self):
        content, assets = self._fixtures()
        asset = assets["ocr"][0]
        response = io.BytesIO(content)
        response.geturl = lambda: asset["url"]
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            models._download(asset, self.root / "good")
        self.assertEqual((self.root / "good").read_bytes(), content)
        response = io.BytesIO(b"x" * len(content))
        response.geturl = lambda: asset["url"]
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(PhotographyError):
            models._download(asset, self.root / "bad")
        with self.assertRaises(PhotographyError):
            models._HTTPSRedirectHandler().redirect_request(None, None, 302, "", {}, "http://example.invalid/model")

    def test_check_rejects_runtime_drift(self):
        with patch.object(models, "_license_gate"), patch.object(models, "asset_paths", return_value={}), \
                patch.object(models, "probe_runtime", return_value={}):
            with self.assertRaises(PhotographyError) as caught:
                models.check_component(ready_profile(), config=self.config)
            self.assertEqual(caught.exception.code, "FEATURE_RUNTIME_INVALID")

    def test_setup_does_not_overwrite_foreign_lock(self):
        directory = models.component_directory("ocr", config=self.config)
        directory.parent.mkdir(parents=True)
        lock = directory.parent / ".setup.lock"
        lock.write_text("not owned")
        with patch.object(models, "probe_runtime", return_value=FAKE_RUNTIME), self.assertRaises(PhotographyError) as caught:
            models.setup_component("ocr", config=self.config)
        self.assertEqual(caught.exception.code, "FEATURE_SETUP_BUSY")
        self.assertEqual(lock.read_text(), "not owned")


FAKE_WORKER = r'''
import io,json,struct,sys,time
mode = sys.argv[1]
def exact(n):
    b=b""
    while len(b)<n:
        x=sys.stdin.buffer.read(n-len(b))
        if not x: raise SystemExit(0)
        b+=x
    return b
def reply(v):
    b=json.dumps(v).encode()
    sys.stdout.buffer.write(struct.pack(">I",len(b))+b);sys.stdout.buffer.flush()
while True:
    n=struct.unpack(">I",exact(4))[0];header=json.loads(exact(n));image=exact(header["image_size"])
    assert not any(k in header for k in ("pixels","base64","image"))
    if header["op"]=="init":
        reply({"ok":True,"ready":True,"model_calls":0});continue
    assert image==b"private-binary-image"
    if mode=="crash": raise SystemExit(9)
    if mode=="hang": time.sleep(60)
    if mode=="bad-json":
        sys.stdout.buffer.write(struct.pack(">I",1)+b"{");sys.stdout.buffer.flush();continue
    if mode=="oversize":
        sys.stdout.buffer.write(struct.pack(">I",2**31));sys.stdout.buffer.flush();continue
    if mode=="error":
        reply({"ok":False,"error":{"code":"FEATURE_IMAGE_INVALID","message":"invalid input"}});continue
    payload={"width":4,"height":3,"complete":True,"objects":[]}
    if mode=="invalid-payload": payload["pixels"]=[1,2,3]
    if mode=="bad-count": count=9
    else: count=0
    reply({"ok":True,"payload":payload,"metadata":{"total_count":count,"returned_count":0,"truncated":False,"onnx_calls":1}})
'''


class FeatureWorkerProtocolTests(unittest.TestCase):
    def provider(self, mode="valid"):
        config = SimpleNamespace(model_cache_root=PROJECT / ".photography-state" / "unused-models")
        provider = VisionProvider(ready_profile(), config=config, python_path=sys.executable)
        for mocked in (
            patch.object(models, "check_component", return_value={"ready": True}),
            patch.object(models, "asset_paths", return_value={"nano.onnx": "unused-local-path"}),
            patch.object(models, "worker_command", return_value=[sys.executable, "-I", "-u", "-c", FAKE_WORKER, mode]),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.addCleanup(provider.close)
        return provider

    def test_binary_request_reuses_worker_and_returns_no_pixels(self):
        with self.provider() as provider:
            first = provider.compute(b"private-binary-image")
            pid = provider._process.pid
            self.assertEqual(first, provider.compute(b"private-binary-image"))
            self.assertEqual(pid, provider._process.pid)
            self.assertEqual(provider.last_metadata["onnx_calls"], 1)
            process = provider._process
        self.assertIsNotNone(process.poll())
        self.assertEqual(set(first), {"width", "height", "complete", "objects"})

    def test_protocol_failure_never_becomes_empty_success(self):
        for mode, expected in [
            ("crash", "FEATURE_WORKER_CRASHED"), ("bad-json", "FEATURE_WORKER_PROTOCOL"),
            ("oversize", "FEATURE_WORKER_PROTOCOL"), ("invalid-payload", "FEATURE_WORKER_PROTOCOL"),
            ("bad-count", "FEATURE_WORKER_PROTOCOL"), ("error", "FEATURE_IMAGE_INVALID"),
        ]:
            with self.subTest(mode=mode):
                provider = self.provider(mode)
                with self.assertRaises(PhotographyError) as caught:
                    provider.compute(b"private-binary-image")
                self.assertEqual(caught.exception.code, expected)
                self.assertIsNone(provider._process)

    def test_timeout_terminates_owned_process(self):
        provider = self.provider("hang")
        provider.__enter__()
        process = provider._process
        provider._timeout = 0.1
        with self.assertRaises(PhotographyError) as caught:
            provider.compute(b"private-binary-image")
        self.assertEqual(caught.exception.code, "FEATURE_WORKER_TIMEOUT")
        self.assertIsNotNone(process.poll())

    def test_url_or_empty_input_is_rejected_without_worker(self):
        provider = self.provider()
        for value in ("https://example.invalid/private.jpg", b"", [], bytearray(b"private")):
            with self.assertRaises(PhotographyError):
                provider.compute(value)
        self.assertIsNone(provider._process)

    def test_binary_framing_rejects_truncated_and_oversized_inputs(self):
        header = json.dumps({"op": "compute", "image_size": 3}).encode()
        frame = struct.pack(">I", len(header)) + header + b"abc"
        self.assertEqual(worker.read_request(io.BytesIO(frame))[1], b"abc")
        with self.assertRaises(EOFError):
            worker.read_request(io.BytesIO(frame[:-1]))
        with self.assertRaises(ValueError):
            worker.read_request(io.BytesIO(struct.pack(">I", worker.MAX_HEADER + 1)))

    def test_readiness_does_not_start_worker(self):
        provider = self.provider()
        self.assertTrue(provider.check_ready()["ready"])
        self.assertIsNone(provider._process)


@unittest.skipUnless(HAS_VISION_RUNTIME, "Optional isolated CPU vision runtime is not installed.")
class FeatureVisionMathTests(unittest.TestCase):
    def test_ocr_pipeline_empty_and_failures_are_distinguished(self):
        import numpy as np
        profile = ready_profile("ocr")
        detector = SimpleNamespace(
            mean=[.5] * 3, std=[.5] * 3, session=Mock(return_value=np.zeros((1, 1, 64, 64))),
            postprocess_op=Mock(return_value=(np.empty((0, 4, 2)), [])), sorted_boxes=lambda boxes: boxes,
        )
        engine = worker.Engine(profile, {})
        engine.engine = SimpleNamespace(text_det=detector)
        payload, metadata = engine._ocr(np.zeros((64, 64, 3), dtype=np.uint8))
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["text"], "")
        self.assertEqual(metadata["onnx_calls"], 1)
        detector.session.side_effect = RuntimeError("model failure")
        with self.assertRaises(RuntimeError):
            engine._ocr(np.zeros((64, 64, 3), dtype=np.uint8))

    def test_ocr_pipeline_raw_text_polygons_unknown_score_and_cap(self):
        import numpy as np
        profile = ready_profile("ocr")
        profile["parameters"]["max_blocks"] = 1
        boxes = np.array([[[0, 0], [30, 0], [30, 20], [0, 20]],
                          [[0, 30], [30, 30], [30, 50], [0, 50]]], dtype=np.float32)
        detector = SimpleNamespace(mean=[.5] * 3, std=[.5] * 3, session=Mock(return_value=np.zeros((1, 1, 64, 64))),
                                   postprocess_op=lambda prediction, size: (boxes, None),
                                   sorted_boxes=lambda value: value)
        recognition = Mock(return_value=SimpleNamespace(txts=["Ａbc", "中文"], scores=[.95, .9]))
        recognition.rec_batch_num = 6
        engine = worker.Engine(profile, {})
        engine.engine = SimpleNamespace(text_det=detector, text_rec=recognition,
                                       crop_text_regions=lambda image, boxes: [image, image])
        payload, metadata = engine._ocr(np.zeros((64, 64, 3), dtype=np.uint8))
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["text"], "Ａbc")
        self.assertEqual(payload["normalized_text"], "abc")
        self.assertIsNone(payload["blocks"][0]["detection_score"])
        self.assertEqual(payload["blocks"][0]["polygon"][1], [30 / 64, 0])
        self.assertEqual(metadata["total_count"], 2)
        validate_payload(profile, payload)
        detector.postprocess_op = lambda prediction, size: (boxes, [.9, .8])
        detector.sorted_boxes = lambda boxes: boxes[::-1]
        scored, _ = engine._ocr(np.zeros((64, 64, 3), dtype=np.uint8))
        self.assertEqual(scored["blocks"][0]["detection_score"], .8)
        recognition.return_value = SimpleNamespace(txts=None, scores=None)
        with self.assertRaises(PhotographyError):
            engine._ocr(np.zeros((64, 64, 3), dtype=np.uint8))

    def test_ocr_explicit_all_three_assets_cpu_and_dictionary_path(self):
        from rapidocr import RapidOCR
        session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
        stage = SimpleNamespace(session=SimpleNamespace(session=session, have_key=lambda: True))
        fake = SimpleNamespace(text_det=stage, text_cls=stage, text_rec=stage)
        paths = {"det.onnx": "local-det", "cls.onnx": "local-cls", "rec.onnx": "local-rec"}
        engine = worker.Engine(ready_profile("ocr"), paths)
        with patch("rapidocr.RapidOCR", return_value=fake) as constructor:
            engine._load()
        p = constructor.call_args.kwargs["params"]
        self.assertEqual(p["Det.model_path"], "local-det")
        self.assertEqual(p["Cls.model_path"], "local-cls")
        self.assertEqual(p["Rec.model_path"], "local-rec")
        self.assertEqual(p["Rec.rec_keys_path"], "local-rec")
        self.assertFalse(p["Global.use_cls"])
        self.assertFalse(p["Global.use_preprocess_img"])
        self.assertFalse(p["Global.use_vertical_padding"])
        self.assertFalse(p["EngineConfig.onnxruntime.use_cuda"])

    def test_decode_image_applies_exif_before_reporting_dimensions(self):
        from PIL import Image
        image = Image.new("RGB", (80, 40), "red")
        exif = Image.Exif()
        exif[274] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif)
        decoded = worker.decode_image(buffer.getvalue(), 10000)
        self.assertEqual(decoded.shape, (80, 40, 3))
        self.assertGreater(int(decoded[0, 0, 2]), int(decoded[0, 0, 0]))
        with self.assertRaises(PhotographyError):
            worker.decode_image(buffer.getvalue(), 10)

    def test_detector_preprocessing_is_bgr_top_left_no_normalization(self):
        import numpy as np
        image = np.zeros((20, 40, 3), dtype=np.uint8)
        image[:] = [10, 20, 30]
        tensor, ratio = worker.yolox_preprocess(image)
        self.assertEqual(tensor.shape, (1, 3, 416, 416))
        self.assertEqual(tensor.dtype, np.float32)
        self.assertEqual(ratio, 10.4)
        self.assertEqual(tensor[0, :, 0, 0].tolist(), [10, 20, 30])
        self.assertEqual(tensor[0, :, 300, 0].tolist(), [114, 114, 114])

    def _prediction(self):
        import numpy as np
        return np.zeros((1, 52 * 52 + 26 * 26 + 13 * 13, 85), dtype=np.float32)

    def test_yolox_objectness_times_class_and_normalized_box(self):
        import numpy as np
        p = default_profile("objects")["parameters"]
        prediction = self._prediction()
        prediction[0, 0, :4] = [10, 10, np.log(4), np.log(4)]
        prediction[0, 0, 4:6] = [.8, .75]
        payload, metadata = worker.yolox_decode(prediction, p, 416, 416, 1)
        self.assertEqual(metadata["total_count"], 1)
        self.assertAlmostEqual(payload["objects"][0]["score"], .6, places=6)
        self.assertEqual(payload["objects"][0]["class_id"], "person")
        self.assertAlmostEqual(payload["objects"][0]["bbox"][0], 64 / 416, places=6)
        validate_payload(default_profile("objects"), payload)

    def test_class_agnostic_nms_and_explicit_truncation(self):
        import numpy as np
        p = default_profile("objects")["parameters"]
        prediction = self._prediction()
        for index, cls in [(0, 0), (1, 1)]:
            prediction[0, index, :4] = [10 - index, 10, np.log(4), np.log(4)]
            prediction[0, index, 4] = .9
            prediction[0, index, 5 + cls] = .9
        payload, _ = worker.yolox_decode(prediction, p, 416, 416, 1)
        self.assertEqual(len(payload["objects"]), 1)
        p["class_agnostic_nms"] = False
        payload, _ = worker.yolox_decode(prediction, p, 416, 416, 1)
        self.assertEqual(len(payload["objects"]), 2)
        p["max_detections"] = 1
        payload, metadata = worker.yolox_decode(prediction, p, 416, 416, 1)
        self.assertFalse(payload["complete"])
        self.assertEqual(metadata, {"total_count": 2, "returned_count": 1, "truncated": True})

    def test_blank_and_invalid_detector_outputs(self):
        import numpy as np
        p = default_profile("objects")["parameters"]
        payload, metadata = worker.yolox_decode(self._prediction(), p, 416, 416, 1)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["objects"], [])
        for invalid in (np.zeros((1, 3, 85)), np.full((1, 3549, 85), np.nan)):
            with self.assertRaises(PhotographyError):
                worker.yolox_decode(invalid, p, 416, 416, 1)


@unittest.skipUnless(
    HAS_VISION_RUNTIME and os.environ.get("SMART_ALBUMS_RUN_SYNTHETIC_VISION") == "1",
    "Real synthetic evaluation is opt-in and needs already prepared local assets.",
)
class RealSyntheticFeatureTests(unittest.TestCase):
    def test_bilingual_ocr_orientation_and_blank_detector_without_downloads(self):
        from PIL import Image, ImageDraw, ImageFont
        root = PROJECT / ".photography-state" / "feature-synthetic"
        config = Config(root / "synthetic.sqlite", model_cache_root=root / "models")
        font_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        font_path = next((font_root / name for name in ("msyh.ttc", "msjh.ttc", "simsun.ttc")
                          if (font_root / name).is_file()), None)
        self.assertIsNotNone(font_path, "A local Chinese font is required for the authorized synthetic test.")
        image = Image.new("RGB", (1100, 360), "white")
        font = ImageFont.truetype(str(font_path), 64)
        draw = ImageDraw.Draw(image)
        draw.text((35, 35), "SMART ALBUMS 2026", font=font, fill="black")
        draw.text((35, 180), "中文测试 图像索引", font=font, fill="black")
        # Store a physically rotated raster; EXIF transpose must restore the text.
        rotated = image.transpose(Image.Transpose.ROTATE_90)
        exif = Image.Exif()
        exif[274] = 6
        buffer = io.BytesIO()
        rotated.save(buffer, format="JPEG", quality=95, exif=exif)
        runtime = models.probe_runtime()
        profile = models.prepared_profile("ocr", runtime)
        with patch.object(models, "_download", side_effect=AssertionError("No downloads in inference")):
            with VisionProvider(profile, config=config) as provider:
                payload = provider.compute(buffer.getvalue())
                pid = provider._process.pid
                self.assertEqual((payload["width"], payload["height"]), image.size)
                self.assertIn("smart albums 2026", payload["normalized_text"])
                self.assertIn("中文测试", payload["normalized_text"])
                self.assertEqual(payload, provider.compute(buffer.getvalue()))
                self.assertEqual(provider._process.pid, pid)
                self.assertEqual(provider.last_metadata["onnx_calls"], 2)
            blank = Image.new("RGB", (640, 360), "white")
            buffer = io.BytesIO()
            blank.save(buffer, format="PNG")
            with patch.dict(os.environ, {"SMART_ALBUMS_YOLOX_LICENSE_REVIEW": "synthetic-evaluation"}):
                with VisionProvider(models.prepared_profile("objects", runtime), config=config) as provider:
                    payload = provider.compute(buffer.getvalue())
                    self.assertEqual((payload["width"], payload["height"]), blank.size)
                    self.assertTrue(payload["complete"])
                    self.assertEqual(provider.last_metadata["onnx_calls"], 1)


if __name__ == "__main__":
    unittest.main()
