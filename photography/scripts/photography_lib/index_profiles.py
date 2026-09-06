"""Built-in image/text space identity; never includes a machine's cache path."""
from __future__ import annotations

from pathlib import Path

REPO = "google/siglip2-base-patch16-224"
REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"

# Large-file digests are the official Hub LFS SHA-256 OIDs, not Git blob OIDs.
# Small JSON digests are SHA-256 of the bytes at this exact revision.
FILES = {
    "config.json": {
        "size": 253,
        "sha256": "fe8b5fe6d5734360678fd71c11c21e1ea3364bd8598d34295d9206335973ffd7",
    },
    "model.safetensors": {
        "size": 1500800904,
        "sha256": "612923381c76ec5a9bed335d1c48827e3f2e506ac31b044b63b2031fadee6a0b",
    },
    "preprocessor_config.json": {
        "size": 394,
        "sha256": "9b36b57ebaf20f09bf4c22100ccc21877ea6bfe5aead0c00c59f8af8ccefacfc",
    },
    "special_tokens_map.json": {
        "size": 636,
        "sha256": "baec30ea10906f16adb8c18af7a34023002c1746542612b8b41c9f09e1351351",
    },
    "tokenizer.json": {
        "size": 34363039,
        "sha256": "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322",
    },
    "tokenizer_config.json": {
        "size": 47164,
        "sha256": "14afe629fe4959b9e0d51e1852b8d9f7ad074f90a1a7125a4fcdd17f06e78fc8",
    },
}

RUNTIME_PACKAGES = {
    "torch": "2.10.0+cpu",
    "transformers": "4.57.6",
    "tokenizers": "0.22.2",
    "huggingface-hub": "0.36.2",
    "hf-xet": "1.2.0",
    "safetensors": "0.7.0",
    "numpy": "2.4.2",
    "Pillow": "12.3.0",
}


def _immutable(*args, **kwargs):
    raise TypeError("Index profiles are immutable.")


class _FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


class _FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable
    __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


def default_profile() -> dict:
    """Return a fresh, JSON-compatible, deeply immutable supported identity."""
    return _freeze({
        "schema": "image-index-profile-v1",
        "model": {
            "repo": REPO,
            "revision": REVISION,
            "class": "SiglipModel",
            "license": "Apache-2.0",
            "source": f"https://huggingface.co/{REPO}/tree/{REVISION}",
            "files": FILES,
        },
        "dimensions": 768,
        "dtype": "float32",
        "byte_order": "little",
        "normalized": True,
        "normalization": "l2-float64-then-float32",
        "quantization": None,
        "runtime": {
            "backend": "pytorch",
            "device": "cpu",
            "dtype": "float32",
            "python": "cpython-3.14-gil-64bit",
            "architecture": "x86_64",
            "packages": RUNTIME_PACKAGES,
            "attention": "eager",
            "batch_size": 1,
            "max_cpu_threads": 2,
            "interop_threads": 1,
            "local_files_only": True,
            "trust_remote_code": False,
        },
        "image": {
            "processor": "SiglipImageProcessor",
            "use_fast": False,
            "input": "stored-jpeg-rgb",
            "do_resize": True,
            "size": {"height": 224, "width": 224},
            "resample": 2,
            "do_rescale": True,
            "rescale_factor": 1 / 255,
            "do_normalize": True,
            "image_mean": [0.5, 0.5, 0.5],
            "image_std": [0.5, 0.5, 0.5],
            "crop": False,
            "pad": False,
            "feature_api": "get_image_features",
        },
        "text": {
            "tokenizer": "GemmaTokenizerFast",
            "use_fast": True,
            "template": "{text}",
            # The checkpoint omits max_position_embeddings: SiglipTextConfig
            # in the pinned Transformers release supplies its default of 64.
            "max_length": 64,
            "add_special_tokens": True,
            "add_bos_token": False,
            "add_eos_token": True,
            "bos_token_id": 2,
            "eos_token_id": 1,
            "pad_token_id": 0,
            "padding": "max_length",
            "padding_side": "right",
            "truncation": False,
            "feature_api": "get_text_features",
            "attention_mask": False,
        },
    })


def default_model_dir(state_dir) -> Path:
    return Path(state_dir).expanduser() / "models" / "siglip2-base-patch16-224" / REVISION
