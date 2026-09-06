"""Standalone Chinese reports bound to one album and its saved preview identities."""
from __future__ import annotations

import base64
import hashlib
import html
import json

from .config import PhotographyError
from .exports import export_path
from .management import snapshot_scope, validate_snapshot_album
from .source_paths import photo_filename


def _text(value):
    return html.escape(str(value), quote=True)


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _preview(item, store):
    from .thumbnails import stored_preview

    try:
        identity = (item.get("content_version"), item.get("thumbnail_profile"), item.get("input_image_hash"))
        if not all(isinstance(value, str) and value for value in identity):
            raise PhotographyError("INVALID_PREVIEW", "This snapshot has no matching saved preview identity.")
        photo = store.photo(item["photo_id"])
        if (photo.get("content_version"), photo.get("thumbnail_profile")) != identity[:2]:
            raise PhotographyError("PHOTO_CHANGED", "Photo changed since this snapshot; no replacement preview is shown.")
        thumbnail = store.thumbnail(item["photo_id"], include_data=False)
        if (thumbnail["content_version"], thumbnail["profile"], thumbnail["image_hash"]) != identity:
            raise PhotographyError("PHOTO_CHANGED", "Preview changed since this snapshot; no replacement preview is shown.")
        data = stored_preview(photo, store)
        if (thumbnail.get("mime_type") != "image/jpeg" or len(data) != thumbnail.get("size_bytes")
                or hashlib.sha256(data).hexdigest() != identity[2]):
            raise PhotographyError("INVALID_PREVIEW", "Stored preview failed its snapshot integrity check.")
        return '<img alt="已保存的照片预览" src="data:image/jpeg;base64,' + base64.b64encode(data).decode("ascii") + '">'
    except (PhotographyError, KeyError) as exc:
        return '<p class="preview-error">预览不可用：' + _text(exc) + "</p>"


def management_report(snapshot, output, *, config, store):
    target = export_path(output, config, store, (".html",))
    cards = []
    with store.read_snapshot():
        validate_snapshot_album(snapshot, store)
        scope = snapshot_scope(snapshot)
        album = snapshot["album"]
        semantic = snapshot["mode"] == "semantic"
        selected = semantic and snapshot.get("display_stage") == "selected"
        items = snapshot.get("results" if semantic else "items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise PhotographyError("INVALID_ARGUMENT", "Snapshot items must be a list of records.")
        for rank, item in enumerate(items, 1):
            image = _preview(item, store)
            title = (item.get("original_relative_path") or item.get("original_absolute_path")
                     or item.get("filename"))
            if not title:
                try:
                    title = photo_filename(store.photo(item["photo_id"]))
                except (PhotographyError, KeyError) as exc:
                    title = str(item.get("photo_id", "照片")) + " · " + str(exc)
            rank_label = "原候选排名 " + _text(item.get("candidate_rank", rank)) if selected else "候选排名 " + str(rank)
            score = '<p class="score">' + rank_label + " · 余弦相似度 " + _text(item.get("score")) + "</p>" if semantic else ""
            cards.append("<article>" + image + '<div class="body"><h2>' + _text(title) + "</h2>" + score
                         + "<details><summary>已保存的元数据与图片语义向量状态</summary><pre>" + _json(item)
                         + "</pre></details></div></article>")
    heading = "Smart Albums · 相册快照 · " + str(album.get("name", ""))
    header = '<header><h1>' + _text(heading) + "</h1>"
    header += "<p>相册文件：" + _text(album.get("database_path", "")) + "</p>"
    header += "<p>相册 UUID：" + _text(album["id"]) + "</p>"
    if "query" in snapshot:
        header += "<p>查询：" + _text(snapshot["query"]) + "</p>"
    if scope["kind"] == "virtual_folders":
        operation = "并集" if scope["match"] == "union" else "交集"
        names = "、".join(folder["name"] for folder in scope["folders"])
        header += "<p>查询时文件夹范围（" + operation + "）：" + _text(names) + "</p>"
        header += "<p>此范围记录查询时的文件夹名称和成员；后续修改不触发重新搜索。</p>"
    else:
        header += "<p>查询范围：整个相册。</p>"
    header += "<p>只读历史快照 · " + _text(snapshot.get("created_at", "")) + "</p>"
    header += "<p>图片语义向量配置（image_embedding）：" + _text(snapshot.get("embedding_configuration")) + "</p>"
    if semantic:
        if selected:
            selection = snapshot.get("selection", {})
            header += ("<h2>筛选后展示</h2><p>从 " + _text(selection.get("candidate_count")) + " 张候选中选择展示 "
                       + str(len(items)) + " 张。选择依据 embedding 相似度信息，不是看图核对或目标检测；仅覆盖本次取回的候选。</p>")
            header += "<p>原检索快照：" + _text(snapshot.get("source_search", {}).get("snapshot_id")) + "</p>"
        else:
            header += "<h2>原始检索候选：尚未筛选</h2><p>这是按相似度排序的候选列表，不表示所有照片都符合查询。默认先由 agent 根据分数和排序筛选；图片只在本地报告中展示给用户，不传给 agent。</p>"
    header += ("<p>元数据、排名和覆盖率均为历史记录，不是实时视图；本次未检查原图。"
               "生成报告时会校验已保存预览的内容版本和哈希，不会替换为已变化的预览。"
               "余弦相似度不是概率。图片语义向量可用不代表已完成对焦或模糊等技术检查。"
               "本页面不会修改相册或索引。</p>")
    if "coverage" in snapshot:
        label = "整个相册" if scope["kind"] == "album" else "所选文件夹范围"
        header += "<h2>" + label + "的图片语义向量覆盖率</h2><pre>" + _json(snapshot["coverage"]) + "</pre>"
    header += "</header>"
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Smart Albums · 相册快照</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.6 system-ui}
header,main,footer{max-width:1280px;margin:auto;padding:24px}h1{font-size:28px}h2{font-size:18px;overflow-wrap:anywhere}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}
article{background:white;border:1px solid #dce1d9;border-radius:12px;overflow:hidden}
img{width:100%;height:280px;object-fit:contain;background:#e6e9e2}.body{padding:20px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.preview-error{padding:20px;color:#8a3b20}
.score{color:#406b67}details{margin-top:16px}
</style></head><body>"""
    empty = "<p>本次候选中未找到适合展示的照片；这不代表未取回的照片都不匹配。</p>" if selected and snapshot.get("selection", {}).get("candidate_count", 0) else "<p>此快照没有照片结果；请结合索引覆盖率判断是否需要建立索引。</p>"
    page += header + "<main>" + ("".join(cards) or empty) + "</main>"
    page += "<footer><details><summary>完整 JSON 快照</summary><pre>" + _json(snapshot)
    page += "</pre></details></footer></body></html>"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return str(target)
