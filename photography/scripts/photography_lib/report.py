"""Export a self-contained, escaped HTML snapshot of real database observations."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from .analysis_schema import fingerprint
from .status import saved_observation
from .thumbnails import stored_preview
from .exports import export_path
from .config import Config, PhotographyError
from .sqlite_storage import now
from .vision import AnalysisConfig


def analysis_report(library_id, output, *, config: Config, store, analysis_config=None, album_id=None):
    output = export_path(output, config, store, (".html",))
    if album_id and library_id:
        raise PhotographyError("INVALID_ARGUMENT", "Select an album or a library, not both.")
    if album_id:
        title = "Photography · " + store.album(album_id)["name"]
        photos = store.photos_for_album(album_id)
    else:
        store.photos(library_id, 1)
        title = "Photography · 照片分析"
        photos = store.photos_for_library(library_id)
    photos = sorted(photos, key=lambda item: (item["relative_path"].casefold(), item["photo_id"]))
    profile = analysis_config.profile() if analysis_config else None
    config_id = fingerprint(profile) if profile else None
    cards = []
    complete = unavailable = pending = needs_update = source_unavailable = 0
    for photo in photos:
        image = ""
        entry, record = saved_observation(photo, store, analysis_config)
        error = None
        try:
            preview = stored_preview(photo, store)
            image = '<img loading="lazy" alt="照片缩略图" src="data:image/jpeg;base64,' + base64.b64encode(preview).decode("ascii") + '">'
        except PhotographyError as exc:
            error = exc.to_dict()
            unavailable += 1
        complete += entry["status"] in ("cached", "saved") and not error
        pending += entry["status"] == "never_analyzed"
        needs_update += entry["status"] == "needs_update"
        source_unavailable += entry["source_status"] != "available"
        status = "预览不可用" if error else "已分析" if entry["status"] in ("cached", "saved") else "需更新分析" if record else "待分析"
        name = html.escape(photo["relative_path"])
        body = ""
        if entry["source_status"] != "available":
            body += '<p class="muted">原图暂不可用、已变化或需要重扫；下方为数据库保存的内容。</p>'
        if record and entry["status"] == "needs_update":
            body += '<p class="muted">以下为历史分析，与当前照片或所选分析配置不匹配。</p>'
        if error:
            body += '<p class="muted">' + html.escape(error["message"]) + '</p>'
        if record:
            body += '<p class="muted">已保存模型：' + html.escape(record.get("model", "未知")) + '</p>'
            data = record["data"]
            body += '<p class="description">' + html.escape(data["visual_description"]) + '</p><dl>'
            for key, label in (("subjects", "主体"), ("scene", "场景"), ("composition", "构图"),
                               ("color", "色彩"), ("lighting", "光线"), ("mood", "氛围"), ("tags", "标签")):
                value = data[key]
                value = " · ".join(value) if isinstance(value, list) else value
                body += f'<dt>{label}</dt><dd>{html.escape(value or "暂无观察")}</dd>'
            body += '</dl><p class="muted">' + html.escape("；".join(data["technical_observations"]["limitations"])) + '</p>'
            body += '<details><summary>完整分析记录</summary><pre>' + html.escape(json.dumps(record, ensure_ascii=False, indent=2)) + '</pre></details>'
        else:
            body += '<p class="muted">尚未分析。导入与浏览不会自动调用模型。</p>'
        search_text = html.escape(photo["relative_path"] + " " + json.dumps(record["data"] if record else {}, ensure_ascii=False), quote=True)
        cards.append(f'<article data-search="{search_text}" data-status="{status}">{image}<div class="body">'
                     f'<span class="badge">{status}</span><h2>{name}</h2>{body}</div></article>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photography · 照片分析</title><style>
*{box-sizing:border-box}body{margin:0;background:#f7f5ef;color:#263a43;font:15px/1.65 system-ui}
header,main{max-width:1440px;margin:auto;padding:24px}header{padding-top:40px}h1{font-size:30px;margin:0}
.muted{color:#64747b;font-size:13px}.summary{font-size:20px;margin:20px 0}input,select{font:inherit;padding:10px;border:1px solid #bfcacb;border-radius:6px;background:white}input{width:min(440px,100%)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px;padding-top:0}
article{background:white;border:1px solid #dde2dc;border-radius:10px;overflow:hidden}article[hidden]{display:none}
img{display:block;width:100%;height:260px;object-fit:contain;background:#eaece6}.body{padding:18px}
h2{font-size:16px;overflow-wrap:anywhere;margin:8px 0}.badge{font-size:12px;border-radius:20px;background:#e7ede4;padding:4px 10px}
dl{display:grid;grid-template-columns:44px 1fr;gap:6px 12px;font-size:13px}dt{color:#66777d}dd{margin:0}
pre{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;color:#315d67}
.description{font-size:15px}.empty{grid-column:1/-1}
</style>'''
    page += f'<header><h1>{html.escape(title)}</h1><p class="muted">数据库只读快照 · 仅显示实际保存的分析结果</p>'
    page += f'<p class="summary">{len(photos)} 张照片 · {complete} 已分析 · {pending} 待分析 · {needs_update} 需更新分析</p>'
    page += f'<p class="muted">{source_unavailable} 张原图需要检查 · {unavailable} 张预览不可用</p>'
    page += '<p class="muted">分析筛选：' + html.escape(profile["model"] if profile else "所有模型的已保存结果，未指定下一次分析模型") + ' · 快照生成：' + html.escape(now()) + '</p>'
    page += '<label for="search">搜索照片或分析内容</label><br><input id="search" placeholder="文件名、主体、色彩或标签"> <label for="status">状态</label> <select id="status"><option value="">全部</option><option>已分析</option><option>待分析</option><option>需更新分析</option><option>预览不可用</option></select><p id="count" class="muted"></p></header><main>'
    page += ''.join(cards) or '<p class="empty">图库中没有照片。</p>'
    page += '''</main><script>
const search=document.getElementById('search'),status=document.getElementById('status');
function filter(){let count=0;const query=search.value.toLocaleLowerCase();
document.querySelectorAll('article').forEach(card=>{card.hidden=!(card.dataset.search.toLocaleLowerCase().includes(query)&&(!status.value||card.dataset.status===status.value));if(!card.hidden)count++;});
document.getElementById('count').textContent='当前显示 '+count+' 张';}
search.addEventListener('input',filter);status.addEventListener('change',filter);filter();
</script></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return {"output": str(output), "photos": len(photos), "analyzed": complete,
            "pending": pending, "needs_update": needs_update, "unavailable": unavailable,
            "source_unavailable": source_unavailable, "analysis_config_id": config_id, "album_id": album_id,
            "model_calls": 0}
