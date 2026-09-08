"""Build a clearly synthetic duplicate gallery for local end-to-end/visual QA.

python tests/duplicates_demo.py --output artifacts/duplicates-demo
Creates only a new fixture directory; never opens a user album.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib import duplicates, feature_inputs
from photography_lib.config import Config
from photography_lib.duplicates_report import duplicate_report
from photography_lib.feature_algorithms import compute_hash
from photography_lib.feature_profiles import default_profile
from photography_lib.ingest import ingestion
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.source_paths import photo_filename


def create_demo(output):
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    originals = output / "synthetic-originals"
    originals.mkdir()
    scene = Image.new("RGB", (720, 480))
    draw = ImageDraw.Draw(scene)
    for y in range(480):
        draw.line((0, y, 720, y), fill=(80 + y // 9, 160 + y // 15, 210 - y // 8))
    draw.ellipse((495, 45, 580, 130), fill="#ffe9a2")
    draw.polygon([(0, 330), (190, 160), (380, 335)], fill="#3b7777")
    draw.polygon([(230, 350), (465, 220), (720, 350)], fill="#5b9684")
    draw.rectangle((0, 340, 720, 480), fill="#adc390")
    draw.rectangle((435, 285, 560, 385), fill="#f4dab4")
    draw.polygon([(415, 285), (493, 222), (580, 285)], fill="#8d5556")
    draw.rectangle((480, 325, 510, 385), fill="#465759")
    scene.save(originals / "01-scene.jpg", quality=95)
    (originals / "02-identical-copy.jpg").write_bytes((originals / "01-scene.jpg").read_bytes())
    scene.resize((360, 240), Image.Resampling.LANCZOS).save(originals / "03-resized-compressed.jpg", quality=60)
    scene.crop((12, 0, 720, 480)).resize((720, 480)).save(originals / "04-small-crop.jpg", quality=85)
    other = Image.new("RGB", (720, 480), "#f3ddc6")
    draw = ImageDraw.Draw(other)
    for x in range(0, 720, 80):
        for y in range(0, 480, 80):
            if (x // 80 + y // 80) % 2:
                draw.rectangle((x, y, x + 79, y + 79), fill="#744d64")
    other.save(originals / "05-different-checkerboard.jpg", quality=90)
    config = Config(output / "synthetic.sqlite", model_cache_root=output / "no-models")
    SQLiteStorage.create(config.database_path).close()
    ingestion(originals, config=config)
    with SQLiteStorage.open(config.database_path, writable=True) as store:
        profile = default_profile("perceptual_hash")
        profile_id = store.put_feature_profile(profile)
        store.set_default_feature_profile("perceptual_hash", profile_id)
        for photo in store.photos():
            payload = compute_hash(store.thumbnail(photo["photo_id"])["data"], profile)
            store.put_feature_result(photo["photo_id"], profile_id,
                                     feature_inputs.manifest_for(photo["photo_id"], profile, store=store), payload)
    with SQLiteStorage.open(config.database_path) as store:
        snapshot = duplicates.scan(store=store, all_photos=True)
        (output / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        duplicate_report(snapshot, output / "report.html", config=config, store=store)
        rows = {row["photo_id"]: row for row in snapshot["inputs"]}
        relations = []
        for group in snapshot["groups"]:
            for pair in duplicates.page(snapshot, store=store, view="pairs", group_id=group["group_id"], limit="all")["pairs"]:
                relations.append({"a": photo_filename(rows[pair["photo_id_a"]]), "b": photo_filename(rows[pair["photo_id_b"]]),
                                  "metric": pair["metric"], "distance": pair["distance"]})
        result = {"source": "Procedurally drawn landscape and checkerboard; synthetic fixtures, not real-photo quality evaluation",
                  "summary": snapshot["summary"], "pairs": relations,
                  "snapshot_bytes": (output / "snapshot.json").stat().st_size,
                  "html_bytes": (output / "report.html").stat().st_size,
                  "original_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals.iterdir()}}
        (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(create_demo(parser.parse_args().output), indent=2))
