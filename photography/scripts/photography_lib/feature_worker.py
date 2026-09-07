"""Private binary-input/JSON-metadata worker; never opens an album database."""
from __future__ import annotations

import io
import json
import os
from collections import defaultdict, deque
from pathlib import Path
import socket
import struct
import sys
import warnings

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photography_lib.config import PhotographyError
from photography_lib import feature_models

MAX_HEADER = 1024 * 1024
MAX_IMAGE = 256 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024


def read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        data = stream.read(count - len(chunks))
        if not data:
            raise EOFError("Incomplete worker frame.")
        chunks.extend(data)
    return bytes(chunks)


def read_request(stream):
    prefix = stream.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        prefix += read_exact(stream, 4 - len(prefix))
    length = struct.unpack(">I", prefix)[0]
    if not 0 < length <= MAX_HEADER:
        raise ValueError("Invalid metadata frame length.")
    header = json.loads(read_exact(stream, length))
    if not isinstance(header, dict) or type(header.get("image_size", 0)) is not int:
        raise ValueError("Invalid request header.")
    size = header.get("image_size", 0)
    if not 0 <= size <= MAX_IMAGE:
        raise ValueError("Image frame exceeds the input limit.")
    return header, read_exact(stream, size)


def write_response(stream, value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE:
        raise PhotographyError("FEATURE_RESULT_LIMIT", "Worker metadata exceeds response byte limit.")
    stream.write(struct.pack(">I", len(encoded)) + encoded)
    stream.flush()


def _deny_network(*args, **kwargs):
    raise PhotographyError("FEATURE_NETWORK_FORBIDDEN", "Inference workers cannot access the network.")


def disable_network():
    socket.create_connection = _deny_network
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network


def decode_image(data, max_pixels):
    from PIL import Image, ImageOps
    import numpy as np
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > max_pixels:
                    raise PhotographyError("FEATURE_IMAGE_INVALID", "Input image exceeds the profile pixel limit.")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
                return np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
    except PhotographyError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotographyError("FEATURE_IMAGE_INVALID", "Cannot decode the supplied local image bytes.") from exc


def yolox_preprocess(image, size=416):
    import cv2
    import numpy as np
    height, width = image.shape[:2]
    ratio = min(size / height, size / width)
    resized = cv2.resize(image, (max(1, int(width * ratio)), max(1, int(height * ratio))),
                         interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[:resized.shape[0], :resized.shape[1]] = resized
    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32), ratio


def yolox_decode(output, parameters, width, height, ratio):
    """Implement the exported YOLOX grid equations and greedy NMS without torch."""
    import numpy as np
    size = parameters["input_size"]
    grids, scales = [], []
    for stride in parameters["strides"]:
        yy, xx = np.indices((size // stride, size // stride))
        grid = np.column_stack((xx.ravel(), yy.ravel()))
        grids.append(grid)
        scales.append(np.full((len(grid), 1), stride))
    grid, scale = np.concatenate(grids), np.concatenate(scales)
    output = np.asarray(output)
    if output.shape != (1, len(grid), 85) or not np.isfinite(output).all():
        raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "YOLOX returned an invalid output tensor.")
    values = output[0].astype(np.float64)
    if ((values[:, 4:] < 0) | (values[:, 4:] > 1)).any():
        raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "YOLOX probabilities are outside [0,1].")
    with np.errstate(over="raise", invalid="raise"):
        try:
            centers = (values[:, :2] + grid) * scale
            extent = np.exp(values[:, 2:4]) * scale
        except FloatingPointError as exc:
            raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "YOLOX box decode overflowed.") from exc
    boxes = np.column_stack((centers - extent / 2, centers + extent / 2)) / ratio
    probabilities = values[:, 4:5] * values[:, 5:]
    if parameters["class_agnostic_nms"]:
        classes = probabilities.argmax(axis=1)
        scores = probabilities[np.arange(len(values)), classes]
        selected = np.flatnonzero(scores > parameters["score_threshold"])
        classes, scores, boxes = classes[selected], scores[selected], boxes[selected]
    else:
        rows, classes = np.nonzero(probabilities > parameters["score_threshold"])
        scores, boxes = probabilities[rows, classes], boxes[rows]
    order = np.argsort(-scores, kind="stable")
    keep = []
    areas = np.prod(np.maximum(0, boxes[:, 2:] - boxes[:, :2] + 1), axis=1)
    while len(order):
        best = int(order[0])
        keep.append(best)
        remaining = order[1:]
        if not len(remaining):
            break
        # The reference ONNX demo uses inclusive pixel extents (+1), before clipping.
        low = np.maximum(boxes[best, :2], boxes[remaining, :2])
        high = np.minimum(boxes[best, 2:], boxes[remaining, 2:])
        intersection = np.prod(np.maximum(0, high - low + 1), axis=1)
        union = areas[best] + areas[remaining] - intersection
        overlaps = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        suppress = overlaps > parameters["nms_threshold"]
        if not parameters["class_agnostic_nms"]:
            suppress &= classes[remaining] == classes[best]
        order = remaining[~suppress]
    objects = []
    for index in keep:
        box = boxes[index].copy()
        box[[0, 2]] = np.clip(box[[0, 2]] / width, 0, 1)
        box[[1, 3]] = np.clip(box[[1, 3]] / height, 0, 1)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        objects.append({"class_id": parameters["labels"][int(classes[index])],
                        "score": float(scores[index]), "bbox": box.tolist()})
    total = len(objects)
    objects = objects[:parameters["max_detections"]]
    complete = len(objects) == total
    return ({"width": width, "height": height, "complete": complete, "objects": objects},
            {"total_count": total, "returned_count": len(objects), "truncated": not complete})


class Engine:
    def __init__(self, profile, paths):
        self.profile, self.paths = profile, paths
        self.engine = None

    def _load(self):
        import onnxruntime as ort
        p = self.profile["parameters"]
        if self.profile["component"] == "objects":
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            self.engine = ort.InferenceSession(self.paths["nano.onnx"], sess_options=options,
                                               providers=["CPUExecutionProvider"])
            if self.engine.get_providers() != ["CPUExecutionProvider"]:
                raise PhotographyError("FEATURE_RUNTIME_INVALID", "Detector enabled a non-CPU provider.")
            return
        from rapidocr import RapidOCR
        params = {
            "Global.use_det": True, "Global.use_cls": p["use_cls"], "Global.use_rec": True,
            "Global.use_preprocess_img": False, "Global.use_vertical_padding": False,
            "Global.return_word_box": False, "Global.return_single_char_box": False,
            "Global.text_score": p["text_score"], "Global.log_level": "error",
            "Det.model_path": self.paths["det.onnx"], "Rec.model_path": self.paths["rec.onnx"],
            "Cls.model_path": self.paths["cls.onnx"],
            # This ONNX embeds its character dictionary. An existing explicit path also
            # prevents the SDK from downloading a fallback if its metadata is damaged.
            "Rec.rec_keys_path": self.paths["rec.onnx"],
            "Det.limit_side_len": p["det_limit_side_len"], "Det.limit_type": p["det_limit_type"],
            "Det.thresh": p["det_thresh"], "Det.box_thresh": p["det_box_thresh"],
            "Det.unclip_ratio": p["det_unclip_ratio"], "Det.max_candidates": p["det_max_candidates"],
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False, "EngineConfig.onnxruntime.use_coreml": False,
        }
        self.engine = RapidOCR(params=params)
        for stage in (self.engine.text_det, self.engine.text_cls, self.engine.text_rec):
            if stage.session.session.get_providers() != ["CPUExecutionProvider"]:
                raise PhotographyError("FEATURE_RUNTIME_INVALID", "OCR enabled a non-CPU provider.")
        if not self.engine.text_rec.session.have_key():
            raise PhotographyError("FEATURE_MODEL_INVALID", "OCR recognition model lacks its embedded dictionary.")

    def compute(self, data):
        from photography_lib.feature_profiles import validate_payload
        p = self.profile["parameters"]
        image = decode_image(data, p.get("max_input_pixels", 80000000))
        if self.engine is None:
            self._load()
        height, width = image.shape[:2]
        if self.profile["component"] == "objects":
            tensor, ratio = yolox_preprocess(image, p["input_size"])
            output = self.engine.run(None, {self.engine.get_inputs()[0].name: tensor})[0]
            payload, metadata = yolox_decode(output, p, width, height, ratio)
            metadata["onnx_calls"] = 1
        else:
            payload, metadata = self._ocr(image)
        return validate_payload(self.profile, payload), metadata

    def _ocr(self, image):
        import numpy as np
        from rapidocr.ch_ppocr_det.utils import DetPreProcess
        from rapidocr.ch_ppocr_rec import TextRecInput
        from photography_lib.feature_profiles import normalize_ocr_text
        p = self.profile["parameters"]
        height, width = image.shape[:2]
        detector = self.engine.text_det
        # The released get_preprocess ignores limit_side_len for "max"; call its
        # preprocessing primitive explicitly so the stored recipe remains truthful.
        tensor = DetPreProcess(p["det_limit_side_len"], p["det_limit_type"],
                               detector.mean, detector.std)(image)
        if tensor is None:
            raise PhotographyError("FEATURE_IMAGE_INVALID", "Image is too narrow for OCR's 32-pixel stride.")
        prediction = detector.session(tensor)
        boxes, detection_scores = detector.postprocess_op(prediction, (height, width))
        score_by_box = defaultdict(deque)
        if detection_scores is not None:
            if len(detection_scores) != len(boxes):
                raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "OCR returned mismatched detection scores.")
            for box, score in zip(boxes, detection_scores):
                if not np.isfinite(score) or not 0 <= score <= 1:
                    raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "OCR returned an invalid detection score.")
                score_by_box[tuple(np.asarray(box).ravel())].append(float(score))
        boxes = detector.sorted_boxes(boxes)
        # RapidOCR sorts boxes without sorting its scores. Reassociate the SDK's
        # genuine detection scores, including duplicate-coordinate boxes.
        sorted_scores = ([score_by_box[tuple(np.asarray(box).ravel())].popleft() for box in boxes]
                         if detection_scores is not None else [None] * len(boxes))
        blocks, calls = [], 1
        if len(boxes):
            crops = self.engine.crop_text_regions(image, boxes)
            if p["use_cls"]:
                classified = self.engine.text_cls(crops)
                calls += (len(crops) + self.engine.text_cls.cls_batch_num - 1) // self.engine.text_cls.cls_batch_num
                if classified.img_list is None or len(classified.img_list) != len(crops):
                    raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "OCR orientation returned incomplete crops.")
                crops = classified.img_list
            recognized = self.engine.text_rec(TextRecInput(img=crops, return_word_box=False))
            calls += (len(crops) + self.engine.text_rec.rec_batch_num - 1) // self.engine.text_rec.rec_batch_num
            if (recognized.txts is None or recognized.scores is None
                    or len(recognized.txts) != len(boxes) or len(recognized.scores) != len(boxes)):
                raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "OCR recognition returned incomplete output.")
            for box, detection_score, text, score in zip(boxes, sorted_scores, recognized.txts, recognized.scores):
                if not isinstance(text, str) or not np.isfinite(score) or not 0 <= score <= 1:
                    raise PhotographyError("FEATURE_MODEL_OUTPUT_INVALID", "OCR returned invalid text or score.")
                if not text.strip() or score < p["text_score"]:
                    continue
                polygon = np.asarray(box, dtype=np.float64) / [width, height]
                blocks.append({"text": text, "polygon": np.clip(polygon, 0, 1).tolist(),
                               "recognition_score": float(score), "detection_score": detection_score})
        total = len(blocks)
        retained, chars = [], 0
        for block in blocks[:p["max_blocks"]]:
            next_chars = chars + len(block["text"]) + bool(retained)
            if next_chars > p["max_text_chars"]:
                break
            retained.append(block)
            chars = next_chars
        blocks = retained
        text = "\n".join(block["text"] for block in blocks)
        complete = len(blocks) == total
        return ({"width": width, "height": height, "complete": complete, "text": text,
                 "normalized_text": normalize_ocr_text(text), "blocks": blocks},
                {"total_count": total, "returned_count": len(blocks), "truncated": not complete, "onnx_calls": calls})


def main():
    if sys.argv[1:] == ["--runtime"]:
        disable_network()
        print(json.dumps(feature_models.runtime_identity(), separators=(",", ":")))
        return
    input_stream = sys.stdin.buffer
    output_stream = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    # Redirect both Python prints and native-library stdout away from the protocol.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    disable_network()
    engine = None
    while True:
        request = read_request(input_stream)
        if request is None:
            return
        header, image = request
        operation = header.get("op")
        try:
            if operation == "init" and engine is None and not image:
                profile = header["profile"]
                feature_models.validate_provider_profile(profile)
                feature_models._license_gate(profile["component"])
                if profile["runtime"] != feature_models.runtime_identity():
                    raise PhotographyError("FEATURE_RUNTIME_INVALID", "Worker identity differs from the registered profile.")
                paths = header["paths"]
                directory = Path(next(iter(paths.values()))).parent
                feature_models._verify_directory(directory, profile["component"])
                expected = {a["name"]: str(directory / a["name"]) for a in feature_models.ASSETS[profile["component"]]}
                if paths != expected:
                    raise PhotographyError("FEATURE_MODEL_INVALID", "Worker received unexpected asset paths.")
                engine = Engine(profile, paths)
                write_response(output_stream, {"ok": True, "ready": True, "model_calls": 0})
            elif operation == "compute" and engine is not None and image:
                payload, metadata = engine.compute(image)
                write_response(output_stream, {"ok": True, "payload": payload, "metadata": metadata})
            elif operation == "close" and not image:
                write_response(output_stream, {"ok": True, "closed": True})
                return
            else:
                raise PhotographyError("FEATURE_WORKER_PROTOCOL", "Unexpected worker operation.")
        except PhotographyError as exc:
            write_response(output_stream, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
            return
        except Exception as exc:
            # SDK failure is an explicit error, never a successful empty result.
            # Do not echo SDK exception text: it can contain image/pixel representations.
            write_response(output_stream, {"ok": False, "error": {
                "code": "FEATURE_WORKER_FAILED", "message": f"Vision worker failed ({type(exc).__name__})."}})
            return


if __name__ == "__main__":
    main()
