"""Create an isolated synthetic gallery and exercise the packaged CLI twice."""
from __future__ import annotations

import html
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parents[1]


def main():
    output_parent = PROJECT / "artifacts"
    output_parent.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="ingest-demo-", dir=output_parent))
    photos, state = output / "photos", output / "state"
    photos.mkdir()
    palette = ("#243547", "#6b705c", "#a26769", "#465d69")
    for index in range(12):
        image = Image.new("RGB", (900, 600), palette[index % len(palette)])
        draw = ImageDraw.Draw(image)
        draw.rectangle((70 + index * 12, 70, 400, 500), fill="#e9dac1")
        draw.ellipse((450, 120 + index * 10, 800, 470 + index * 10), fill="#d3a75f")
        draw.text((25, 20), f"SYNTHETIC TEST IMAGE {index + 1:02d}", fill="white")
        image.save(photos / f"test-{index + 1:02d}.jpg")
    command = [sys.executable, str(PROJECT / "photography" / "scripts" / "photography.py"),
               "--state-dir", str(state)]

    def run(*arguments):
        completed = subprocess.run(command + list(arguments), check=True, capture_output=True,
                                   text=True, encoding="utf-8", cwd=output)
        return json.loads(completed.stdout)

    first = run("ingest", str(photos), "--album-name", "合成照片演示")
    second = run("ingest", str(photos), "--album-name", "合成照片演示")
    records = run("photos", "--library-id", first["library_id"])["items"]
    if (first["added"], first["failed"], second["unchanged"], second["changed_photo_ids"]) != (12, 0, 12, []):
        raise RuntimeError("The two-scan demonstration did not produce the expected counts.")
    report = {"first_scan": first, "second_scan": second, "photos_directory": str(photos),
              "state_directory": str(state)}
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = []
    for photo in sorted(records, key=lambda item: item["relative_path"]):
        preview = (Path("previews") / (photo["photo_id"] + ".jpg")).as_posix()
        run("thumbnail", photo["photo_id"], "--output", str(output / preview))
        meta = photo["metadata"]
        cards.append(f'<figure><img src="{html.escape(preview)}" alt="Synthetic ingestion test">'
                     f'<figcaption>{html.escape(photo["relative_path"])} · '
                     f'{meta["thumbnail_width"]} × {meta["thumbnail_height"]}</figcaption></figure>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>导入演示</title>
<style>body{font:16px system-ui;margin:40px auto;padding:0 24px;max-width:1200px;background:#faf8f3;color:#243547}
h1{font-size:32px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}
figure{margin:0;background:white;border:1px solid #ddd;border-radius:8px;overflow:hidden}
img{width:100%;display:block}figcaption{padding:12px;font-size:14px}.result{padding:20px;background:#e7eddf;margin:24px 0}</style>
<h1>Photography · 导入演示</h1><p>下方均为程序生成的测试图案，用于验证导入与缩略图，不是真实摄影作品。</p>
<div class="result">首次扫描：新增 12，失败 0。再次扫描：未变 12，待后续处理 0。</div><main>'''
    (output / "index.html").write_text(page + "".join(cards) + "</main></html>", encoding="utf-8")
    print(json.dumps({"output_directory": str(output), "preview": str(output / "index.html"),
                      "first_added": first["added"], "second_unchanged": second["unchanged"]}, indent=2))


if __name__ == "__main__":
    main()
