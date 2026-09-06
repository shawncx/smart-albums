from __future__ import annotations

from contextlib import nullcontext
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import shutil
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib.fingerprints import fingerprint
from photography_lib.config import PhotographyError
from photography_lib import index_profiles
from photography_lib import siglip_embedding as adapter
from photography_lib.image_vectors import pack_vector, unpack_vector, validate_vector


class FakeTensor:
    def __init__(self, values):
        self.values = values
        self.moves = []
        self.shape = (len(values), len(values[0])) if values and isinstance(values[0], list) else (len(values),)

    def to(self, **kwargs):
        self.moves.append(kwargs)
        return self

    def detach(self):
        return self

    def __getitem__(self, key):
        return FakeTensor(self.values[key])

    def tolist(self):
        return self.values


class FakeTokenizer:
    add_bos_token = False
    add_eos_token = True
    bos_token_id = 2
    eos_token_id = 1
    pad_token_id = 0
    padding_side = "right"

    def __init__(self):
        self.calls = []

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        ids = [11] * len(text) + [1]
        if kwargs["padding"] == "max_length":
            ids += [0] * (kwargs["max_length"] - len(ids))
            return {"input_ids": FakeTensor([ids])}
        return {"input_ids": ids}


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class ProfileAndVectorTests(unittest.TestCase):
    def test_profile_is_fresh_immutable_json_and_path_independent(self):
        first, second = index_profiles.default_profile(), index_profiles.default_profile()
        self.assertIsNot(first, second)
        self.assertIsNot(first["model"], second["model"])
        self.assertEqual(first, json.loads(json.dumps(second)))
        with self.assertRaises(TypeError):
            first["dimensions"] = 7
        with self.assertRaises(TypeError):
            first["model"]["files"]["config.json"]["size"] = 1
        with self.assertRaises(TypeError):
            first["image"]["image_mean"].append(1)
        one = adapter.SiglipEncoder(PROJECT / "one").profile()
        two = adapter.SiglipEncoder(PROJECT / "two").profile()
        self.assertEqual(fingerprint(one), fingerprint(two))
        self.assertNotEqual(index_profiles.default_model_dir("one"), index_profiles.default_model_dir("two"))

    def test_official_identity_and_manifest(self):
        profile = index_profiles.default_profile()
        self.assertEqual(profile["model"]["revision"], "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2")
        self.assertEqual(profile["model"]["repo"], "google/siglip2-base-patch16-224")
        self.assertEqual(profile["dimensions"], 768)
        self.assertEqual(profile["image"]["size"], {"height": 224, "width": 224})
        self.assertEqual(profile["image"]["resample"], 2)
        self.assertFalse(profile["image"]["use_fast"])
        self.assertEqual(profile["image"]["image_std"], [0.5] * 3)
        self.assertEqual(profile["text"]["max_length"], 64)
        self.assertFalse(profile["text"]["add_bos_token"])
        self.assertTrue(profile["text"]["add_eos_token"])
        self.assertEqual(profile["model"]["files"]["model.safetensors"]["size"], 1500800904)
        self.assertEqual(profile["model"]["files"]["tokenizer.json"]["size"], 34363039)
        self.assertNotIn("tokenizer.model", profile["model"]["files"])
        for item in profile["model"]["files"].values():
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(item["size"], 0)

    def test_requirements_match_runtime_identity(self):
        requirements = (PROJECT / "photography" / "requirements-index.txt").read_text()
        for package, version in index_profiles.RUNTIME_PACKAGES.items():
            self.assertIn(f"{package}=={version}", requirements)
        self.assertIn("https://download.pytorch.org/whl/cpu", requirements)

    def test_unknown_profile_is_rejected_even_if_dimensions_match(self):
        profile = json.loads(json.dumps(index_profiles.default_profile()))
        profile["image"]["crop"] = True
        for unsupported in (profile, {"dimensions": 768}, [], "unknown"):
            with self.subTest(profile=unsupported), self.assertRaises(PhotographyError) as caught:
                adapter.SiglipEncoder(PROJECT, unsupported)
            self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
        supported = json.loads(json.dumps(index_profiles.default_profile()))
        self.assertEqual(adapter.SiglipEncoder(PROJECT, supported).profile(), supported)

    def test_little_endian_float32_unit_roundtrip(self):
        blob = pack_vector([0.6, 0.8], 2)
        self.assertEqual(blob, struct.pack("<2f", 0.6, 0.8))
        self.assertAlmostEqual(unpack_vector(blob, 2)[0], 0.6)
        full = [1.0] + [0.0] * 767
        self.assertEqual(len(pack_vector(full, 768)), 3072)
        self.assertEqual(unpack_vector(pack_vector(full, 768), 768), full)

    def test_invalid_vectors_are_never_persistable(self):
        cases = [
            ([], 2), ([1], 2), ([1, 1], 2), ([0, 0], 2),
            ([float("nan"), 0], 2), ([float("inf"), 0], 2),
            (["1", 0], 2), ([True, 0], 2), ([1, 0], 0), ([1], True),
            ([1e308, 1e308], 2), (None, 2),
        ]
        for vector, dimensions in cases:
            with self.subTest(vector=vector, dimensions=dimensions), self.assertRaises(PhotographyError) as caught:
                pack_vector(vector, dimensions)
            self.assertEqual(caught.exception.code, "INDEX_VECTOR_INVALID")
        self.assertEqual(validate_vector([3, 4], 2, normalized=False), [3.0, 4.0])

    def test_invalid_blob_size_type_endian_and_values(self):
        for blob, dimensions in [
            (b"", 768), (bytes(3071), 768), (bytes(3073), 768),
            ("abcd", 1), (None, 1), (b"\0" * 4, -1),
            (struct.pack("<f", float("nan")), 1),
            (struct.pack(">f", 1), 1), (struct.pack("<f", 0), 1),
        ]:
            with self.subTest(blob=blob, dimensions=dimensions), self.assertRaises(PhotographyError):
                unpack_vector(blob, dimensions)


class LocalFixtureTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / "tests" / f".siglip-fixture-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root))
        self.data = {
            "config.json": b'{"model_type":"siglip"}',
            "model.safetensors": b"synthetic test bytes, never loaded",
            "tokenizer.json": b'{"fake":true}',
        }
        self.manifest = {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in self.data.items()
        }
        self.files_patch = patch.object(index_profiles, "FILES", self.manifest)
        self.files_patch.start()
        self.addCleanup(self.files_patch.stop)
        self.runtime_patch = patch.object(adapter, "_check_runtime", return_value={"test": "mocked"})
        self.runtime = self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.network_patch = patch.object(adapter.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden"))
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def write_model(self, directory=None):
        directory = directory or self.root
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in self.data.items():
            (directory / name).write_bytes(data)

    def fake_download(self, name, destination, expected):
        destination.write_bytes(self.data[name])

    def fake_runtime(self):
        tokenizer = FakeTokenizer()
        features = FakeTensor([[3.0, 4.0] + [0.0] * 766])
        model = Mock()
        model.config = SimpleNamespace(
            text_config=SimpleNamespace(max_position_embeddings=64, hidden_size=768),
            vision_config=SimpleNamespace(hidden_size=768, image_size=224),
        )
        model.get_image_features.return_value = features
        model.get_text_features.return_value = features
        processor = Mock(return_value={"pixel_values": FakeTensor([[1.0]])})
        model_class = Mock(from_pretrained=Mock(return_value=model))
        processor_class = Mock(from_pretrained=Mock(return_value=processor))
        tokenizer_class = Mock(from_pretrained=Mock(return_value=tokenizer))
        torch = SimpleNamespace(
            float32="float32", set_num_threads=Mock(), get_num_interop_threads=Mock(return_value=2),
            set_num_interop_threads=Mock(), version=SimpleNamespace(cuda=None),
            inference_mode=Mock(side_effect=nullcontext),
        )
        return (torch, model_class, processor_class, tokenizer_class), model, processor, tokenizer

    def test_ready_checks_files_and_metadata_without_loading(self):
        self.write_model()
        with patch.object(adapter, "_load_runtime", side_effect=AssertionError("Model load forbidden")):
            encoder = adapter.SiglipEncoder(self.root)
            self.assertEqual(encoder.check_ready()["status"], "ready")
            self.assertEqual((encoder.calls, encoder.load_seconds), (0, 0.0))
        self.runtime.assert_called_once()
        self.network.assert_not_called()

    def test_missing_and_checksum_corrupt_files_block_loading(self):
        encoder = adapter.SiglipEncoder(self.root)
        with self.assertRaises(PhotographyError) as caught:
            encoder.check_ready()
        self.assertEqual(caught.exception.code, "INDEX_MODEL_MISSING")
        self.write_model()
        bad = self.root / "model.safetensors"
        bad.write_bytes(b"x" * bad.stat().st_size)
        with patch.object(adapter, "_load_runtime") as load:
            with self.assertRaises(PhotographyError) as caught:
                encoder.encode_text("query")
        self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
        self.assertEqual(encoder.calls, 1)
        self.assertEqual(encoder.load_seconds, 0)
        load.assert_not_called()

    def test_setup_atomic_snapshot_and_all_file_reuse(self):
        with patch.object(adapter, "_download_file", side_effect=self.fake_download) as download:
            first = adapter.setup_model(self.root)
            self.assertEqual(first["status"], "ready")
            self.assertEqual(set(first["downloaded_files"]), set(self.data))
            self.assertEqual(first["profile_id"], fingerprint(first["profile"]))
            before = (self.root / "CURRENT").read_bytes()
            second = adapter.setup_model(self.root)
            self.assertEqual(second["downloaded_files"], [])
            self.assertEqual(set(second["reused_files"]), set(self.data))
            self.assertEqual(download.call_count, len(self.data))
            self.assertEqual((self.root / "CURRENT").read_bytes(), before)
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")
        self.network.assert_not_called()

    def test_setup_keeps_published_files_below_windows_path_limit(self):
        directory = self.root / ("nested-" + "x" * max(1, 150 - len(str(self.root)) - 8))
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            adapter.setup_model(directory)
        snapshot = adapter._snapshot_dir(directory)
        self.assertEqual(len(snapshot.name), 32)
        for name in self.manifest:
            self.assertLess(len(str(snapshot / name)), 260)
        self.assertEqual(adapter.SiglipEncoder(directory).check_ready()["status"], "ready")

    def test_legacy_generation_names_remain_readable(self):
        generation = fingerprint(index_profiles.default_profile()) + "-" + uuid.uuid4().hex
        self.write_model(self.root / ".snapshots" / generation)
        (self.root / "CURRENT").write_text(generation + "\n", encoding="ascii")
        with patch.object(adapter, "_download_file", side_effect=AssertionError("Must reuse old installation")):
            result = adapter.setup_model(self.root)
        self.assertEqual(result["downloaded_files"], [])
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")

    def test_published_directory_is_checked_before_pointer_changes(self):
        validate = adapter._validate_files

        def check_directory(directory):
            if directory.parent.name == ".snapshots":
                raise PhotographyError("INDEX_MODEL_MISSING", "Synthetic unreadable publication path")
            return validate(directory)

        with patch.object(adapter, "_download_file", side_effect=self.fake_download), \
                patch.object(adapter, "_validate_files", side_effect=check_directory), self.assertRaises(PhotographyError):
            adapter.setup_model(self.root)
        self.assertFalse((self.root / "CURRENT").exists())

    def test_interrupted_setup_reuses_verified_stage(self):
        count = 0

        def interrupted(name, destination, expected):
            nonlocal count
            count += 1
            if count == 2:
                raise PhotographyError("INDEX_DOWNLOAD_FAILED", "synthetic interruption")
            self.fake_download(name, destination, expected)

        with patch.object(adapter, "_download_file", side_effect=interrupted):
            with self.assertRaises(PhotographyError):
                adapter.setup_model(self.root)
        self.assertFalse((self.root / "CURRENT").exists())
        with patch.object(adapter, "_download_file", side_effect=self.fake_download) as download:
            result = adapter.setup_model(self.root)
        self.assertEqual(result["reused_files"], ["config.json"])
        self.assertEqual(download.call_count, len(self.data) - 1)
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")

    def test_failed_repair_preserves_existing_publication_and_verified_files(self):
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            adapter.setup_model(self.root)
        before = (self.root / "CURRENT").read_bytes()
        old = adapter._snapshot_dir(self.root)
        (old / "tokenizer.json").write_bytes(b"corrupt")
        preserved = (old / "model.safetensors").read_bytes()
        with patch.object(adapter, "_download_file", side_effect=PhotographyError("INDEX_DOWNLOAD_FAILED", "offline")):
            with self.assertRaises(PhotographyError):
                adapter.setup_model(self.root)
        self.assertEqual((self.root / "CURRENT").read_bytes(), before)
        self.assertEqual((old / "model.safetensors").read_bytes(), preserved)
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            result = adapter.setup_model(self.root)
        self.assertEqual(result["downloaded_files"], ["tokenizer.json"])
        self.assertNotEqual((self.root / "CURRENT").read_bytes(), before)
        self.assertEqual((old / "model.safetensors").read_bytes(), preserved)
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")

    def test_corrupt_download_cannot_publish(self):
        def corrupt(name, destination, expected):
            destination.write_bytes(b"wrong")
        with patch.object(adapter, "_download_file", side_effect=corrupt):
            with self.assertRaises(PhotographyError) as caught:
                adapter.setup_model(self.root)
        self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
        self.assertFalse((self.root / "CURRENT").exists())

    def test_setup_repairs_corrupt_pointer_but_normal_load_does_not_fallback(self):
        self.write_model()
        (self.root / "CURRENT").write_text("../outside")
        with self.assertRaises(PhotographyError):
            adapter.SiglipEncoder(self.root).check_ready()
        # Valid flat files can be republished, but never mask a broken CURRENT.
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            adapter.setup_model(self.root)
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")

    def test_interrupted_publication_preserves_usable_old_model_and_reuses_generation(self):
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            adapter.setup_model(self.root)
        old_pointer = (self.root / "CURRENT").read_bytes()
        old_directory = adapter._snapshot_dir(self.root)
        old_data = {name: (old_directory / name).read_bytes() for name in self.data}
        # Simulate a subsequent shipped manifest, without using real model data.
        self.data["config.json"] = b'{"model_type":"siglip","new":true}'
        data = self.data["config.json"]
        self.manifest["config.json"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        with patch.object(adapter, "_download_file", side_effect=self.fake_download), \
                patch.object(adapter, "_publish_current", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(PhotographyError):
                adapter.setup_model(self.root)
        self.assertEqual((self.root / "CURRENT").read_bytes(), old_pointer)
        self.assertEqual({name: (old_directory / name).read_bytes() for name in self.data}, old_data)
        with patch.object(adapter, "_download_file", side_effect=AssertionError("Must reuse verified generation")):
            result = adapter.setup_model(self.root)
        self.assertEqual(result["downloaded_files"], [])
        self.assertEqual(set(result["reused_files"]), set(self.data))
        self.assertEqual(adapter.SiglipEncoder(self.root).check_ready()["status"], "ready")

    def test_setup_lock_rejects_a_second_setup(self):
        with adapter._setup_lock(self.root):
            with self.assertRaises(PhotographyError) as caught:
                adapter.setup_model(self.root)
        self.assertEqual(caught.exception.code, "INDEX_SETUP_BUSY")
        with patch.object(adapter, "_download_file", side_effect=self.fake_download):
            self.assertEqual(adapter.setup_model(self.root)["status"], "ready")

    def test_download_uses_fixed_revision_and_hashes(self):
        name = "config.json"
        self.network.side_effect = None
        self.network.return_value = Response(self.data[name])
        destination = self.root / name
        adapter._download_file(name, destination, self.manifest[name])
        self.assertEqual(destination.read_bytes(), self.data[name])
        request = self.network.call_args.args[0]
        self.assertIn(index_profiles.REVISION, request.full_url)
        self.assertIn(index_profiles.REPO, request.full_url)
        self.assertFalse((self.root / (name + ".part")).exists())

    def test_partial_download_resumes_and_ignored_range_restarts(self):
        name = "config.json"
        destination = self.root / name
        partial = self.root / (name + ".part")
        for status in (206, 200):
            partial.write_bytes(self.data[name][:5])
            self.network.side_effect = None
            body = self.data[name][5:] if status == 206 else self.data[name]
            headers = {"Content-Range": f"bytes 5-{len(self.data[name]) - 1}/{len(self.data[name])}"}
            self.network.return_value = Response(body, status, headers)
            adapter._download_file(name, destination, self.manifest[name])
            self.assertEqual(self.network.call_args.args[0].get_header("Range"), "bytes=5-")
            self.assertEqual(destination.read_bytes(), self.data[name])

    def test_download_failure_keeps_partial_and_rejects_wrong_digest(self):
        name = "config.json"
        destination = self.root / name
        partial = self.root / (name + ".part")
        partial.write_bytes(self.data[name][:5])
        self.network.side_effect = urllib.error.URLError("synthetic offline")
        with self.assertRaises(PhotographyError) as caught:
            adapter._download_file(name, destination, self.manifest[name])
        self.assertEqual(caught.exception.code, "INDEX_DOWNLOAD_FAILED")
        self.assertEqual(partial.read_bytes(), self.data[name][:5])
        self.network.side_effect = None
        self.network.return_value = Response(b"x" * len(self.data[name]))
        with self.assertRaises(PhotographyError) as caught:
            adapter._download_file(name, destination, self.manifest[name])
        self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
        self.assertFalse(destination.exists())
        self.assertFalse(partial.exists())

    def test_features_are_local_cpu_single_image_and_model_is_reused(self):
        self.write_model()
        runtime, model, processor, tokenizer = self.fake_runtime()
        image = object()
        with patch.object(adapter, "_load_runtime", return_value=runtime) as load, \
                patch.object(adapter, "_decode_jpeg", side_effect=lambda data: nullcontext(image)):
            encoder = adapter.SiglipEncoder(self.root)
            first = encoder.encode_image(b"synthetic jpeg")
            second = encoder.encode_text("湖边")
            third = encoder.encode_image(b"second synthetic jpeg")
        self.assertEqual(encoder.calls, 3)
        self.assertGreater(encoder.load_seconds, 0)
        self.assertEqual(first.vector, second.vector)
        self.assertEqual(second.vector, third.vector)
        self.assertEqual(len(first.vector), 768)
        self.assertAlmostEqual(first.vector[0], 0.6)
        self.assertIsNone(first.token_count)
        self.assertEqual(second.token_count, 3)
        self.assertGreater(first.elapsed_seconds, 0)
        self.assertGreater(second.elapsed_seconds, 0)
        load.assert_called_once()
        runtime[0].set_num_threads.assert_called_once_with(2)
        runtime[0].set_num_interop_threads.assert_called_once_with(1)
        for loader in runtime[1:]:
            kwargs = loader.from_pretrained.call_args.kwargs
            self.assertIs(kwargs["local_files_only"], True)
            self.assertIs(kwargs["trust_remote_code"], False)
        self.assertEqual(runtime[1].from_pretrained.call_args.kwargs["torch_dtype"], "float32")
        self.assertTrue(runtime[1].from_pretrained.call_args.kwargs["use_safetensors"])
        self.assertEqual(runtime[1].from_pretrained.call_args.kwargs["attn_implementation"], "eager")
        model.to.assert_called_once_with(device="cpu", dtype="float32")
        model.eval.assert_called_once()
        model.requires_grad_.assert_called_once_with(False)
        self.assertEqual(model.get_image_features.call_count, 2)
        self.assertEqual(model.get_text_features.call_count, 1)
        processor.assert_called_with(images=image, return_tensors="pt")
        self.assertEqual(tokenizer.calls[0][0], "湖边")
        self.assertEqual(tokenizer.calls[1][0], "湖边")
        self.assertTrue(tokenizer.calls[0][1]["add_special_tokens"])
        self.assertFalse(tokenizer.calls[0][1]["truncation"])
        self.assertFalse(tokenizer.calls[1][1]["truncation"])
        self.assertEqual(tokenizer.calls[1][1]["padding"], "max_length")
        ids = model.get_text_features.call_args.kwargs["input_ids"].values[0]
        self.assertEqual(ids[:4], [11, 11, 1, 0])
        self.assertEqual(len(ids), 64)
        self.assertNotIn("attention_mask", model.get_text_features.call_args.kwargs)
        self.network.assert_not_called()

    def test_exact_context_boundary_counts_eos_and_never_truncates(self):
        self.write_model()
        runtime, model, _, tokenizer = self.fake_runtime()
        with patch.object(adapter, "_load_runtime", return_value=runtime):
            encoder = adapter.SiglipEncoder(self.root)
            self.assertEqual(encoder.encode_text("x" * 63).token_count, 64)
            with self.assertRaises(PhotographyError) as caught:
                encoder.encode_text("x" * 64)
        self.assertEqual(caught.exception.code, "QUERY_TOO_LONG")
        self.assertEqual(caught.exception.details, {"token_count": 65, "max_tokens": 64})
        self.assertEqual(model.get_text_features.call_count, 1)
        self.assertEqual(len(tokenizer.calls), 3)
        self.assertFalse(tokenizer.calls[-1][1]["truncation"])
        self.assertEqual(encoder.calls, 2)

    def test_blank_query_counts_attempt_without_loading(self):
        with patch.object(adapter, "_load_runtime") as load:
            encoder = adapter.SiglipEncoder(self.root)
            for query in ("", " \t\n", None):
                with self.assertRaises(PhotographyError) as caught:
                    encoder.encode_text(query)
                self.assertEqual(caught.exception.code, "INVALID_QUERY")
        self.assertEqual(encoder.calls, 3)
        load.assert_not_called()

    def test_jpeg_decode_is_rgb_and_rejects_other_formats_before_loading(self):
        from PIL import Image
        jpeg = io.BytesIO()
        Image.new("L", (13, 7), 80).save(jpeg, format="JPEG")
        original = jpeg.getvalue()
        with adapter._decode_jpeg(original) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (13, 7))
        self.assertEqual(jpeg.getvalue(), original)
        png = io.BytesIO()
        Image.new("RGB", (13, 7)).save(png, format="PNG")
        for invalid in (png.getvalue(), b"not jpeg", b""):
            with patch.object(adapter, "_load_runtime") as load:
                encoder = adapter.SiglipEncoder(self.root)
                with self.assertRaises(PhotographyError) as caught:
                    encoder.encode_image(invalid)
            self.assertEqual(caught.exception.code, "INVALID_PREVIEW")
            self.assertEqual((encoder.calls, encoder.load_seconds), (1, 0))
            load.assert_not_called()

    def test_incompatible_loaded_context_and_tokenizer_are_rejected(self):
        self.write_model()
        for kind in ("context", "tokenizer", "cuda"):
            runtime, model, _, tokenizer = self.fake_runtime()
            if kind == "context":
                model.config.text_config.max_position_embeddings = 128
            elif kind == "tokenizer":
                tokenizer.add_bos_token = True
            else:
                runtime[0].version.cuda = "unexpected"
            with patch.object(adapter, "_load_runtime", return_value=runtime):
                with self.assertRaises(PhotographyError) as caught:
                    adapter.SiglipEncoder(self.root).encode_text("test")
            self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
            model.get_text_features.assert_not_called()

    def test_invalid_feature_shape_nonfinite_and_zero_are_rejected(self):
        self.write_model()
        for raw in ([[1, 0]], [[0.0] * 768], [[float("nan")] + [0.0] * 767]):
            runtime, model, _, _ = self.fake_runtime()
            model.get_text_features.return_value = FakeTensor(raw)
            with patch.object(adapter, "_load_runtime", return_value=runtime):
                with self.assertRaises(PhotographyError) as caught:
                    adapter.SiglipEncoder(self.root).encode_text("test")
            self.assertIn(caught.exception.code, {"INDEX_ENCODING_FAILED", "INDEX_VECTOR_INVALID"})

    def test_encoding_and_load_failures_surface_without_fallback(self):
        self.write_model()
        runtime, model, _, _ = self.fake_runtime()
        model.get_text_features.side_effect = RuntimeError("synthetic CPU failure")
        with patch.object(adapter, "_load_runtime", return_value=runtime):
            encoder = adapter.SiglipEncoder(self.root)
            with self.assertRaises(PhotographyError) as caught:
                encoder.encode_text("test")
        self.assertEqual(caught.exception.code, "INDEX_ENCODING_FAILED")
        self.assertEqual(encoder.calls, 1)
        runtime[1].from_pretrained.side_effect = OSError("synthetic missing weights")
        with patch.object(adapter, "_load_runtime", return_value=runtime):
            with self.assertRaises(PhotographyError) as caught:
                adapter.SiglipEncoder(self.root).encode_text("test")
        self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")
        self.network.assert_not_called()


class RuntimeMetadataTests(unittest.TestCase):
    def test_metadata_modules_have_not_imported_optional_runtime(self):
        for name in ("torch", "transformers", "tokenizers", "huggingface_hub", "numpy"):
            self.assertNotIn(name, sys.modules)

    def test_missing_dependency_and_wrong_identity_are_explicit(self):
        with patch.object(importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError("torch")):
            with self.assertRaises(PhotographyError) as caught:
                adapter._check_runtime()
        self.assertEqual(caught.exception.code, "INDEX_DEPENDENCY_MISSING")
        self.assertIn(".venv-index", str(caught.exception))
        with patch.object(importlib.metadata, "version", return_value="wrong"):
            with self.assertRaises(PhotographyError) as caught:
                adapter._check_runtime()
        self.assertEqual(caught.exception.code, "INDEX_MODEL_INVALID")

    def test_supported_package_metadata_never_imports_runtime(self):
        with patch.object(importlib.metadata, "version", side_effect=index_profiles.RUNTIME_PACKAGES.__getitem__), \
                patch.object(adapter, "_load_runtime", side_effect=AssertionError("No load")):
            self.assertEqual(adapter._check_runtime(), index_profiles.RUNTIME_PACKAGES)

    def test_wrong_python_has_precise_environment_guidance(self):
        with patch.object(adapter.sys, "version_info", (3, 13, 0)):
            with self.assertRaises(PhotographyError) as caught:
                adapter._check_runtime()
        self.assertEqual(caught.exception.code, "INDEX_DEPENDENCY_MISSING")
        self.assertIn("3.14", str(caught.exception))

    def test_unsupported_free_threaded_abi_is_rejected_before_loading(self):
        with patch.object(adapter.sysconfig, "get_config_var", return_value=1):
            with self.assertRaises(PhotographyError) as caught:
                adapter._check_runtime()
        self.assertEqual(caught.exception.code, "INDEX_DEPENDENCY_MISSING")


if __name__ == "__main__":
    unittest.main()
