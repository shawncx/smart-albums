"""Standalone ranked search snapshot; selections export exact IDs for album addition."""
from __future__ import annotations

import base64
import html
import json

from .config import PhotographyError
from .exports import export_path
from .thumbnails import stored_preview


def search_report(result, output, *, config, store):
    target = export_path(output, config, store, (".html",))
    cards = []
    for rank, item in enumerate(result["results"], 1):
        photo = store.photo(item["photo_id"])
        image = '<p>缩略图暂不可用</p>'
        try:
            if photo["content_version"] != item["content_version"]:
                raise PhotographyError("PHOTO_CHANGED", "Photo changed after search.")
            image = '<img alt="照片缩略图" src="data:image/jpeg;base64,' + base64.b64encode(stored_preview(photo, store)).decode("ascii") + '">'
        except PhotographyError:
            pass
        cards.append(f'<article>{image}<div class="body"><label><input type="checkbox" value="{html.escape(item["photo_id"], quote=True)}">选择第 {rank} 张</label>'
            f'<span class="score">相似度 {item["score"]:.3f}</span><h2>{html.escape(item["relative_path"])}</h2>'
            f'<p>{html.escape(item["description"])}</p><details><summary>照片标识</summary><code>{html.escape(item["photo_id"])}</code></details></div></article>')
    coverage = result["coverage"]
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photography · 语义搜索</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.7 system-ui}header,main,footer{max-width:1320px;margin:auto;padding:24px}h1{font-size:30px;margin:0}h2{font-size:17px;overflow-wrap:anywhere}.muted{color:#61747c;font-size:13px}.summary{font-size:20px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px}article{background:white;border:1px solid #dce1d9;border-radius:12px;overflow:hidden}img{width:100%;height:280px;object-fit:contain;background:#e6e9e2}.body{padding:20px}.score{display:block;color:#406b67;font-size:13px}button{background:#294f54;color:white;border:0;border-radius:6px;padding:12px 18px;font:inherit;cursor:pointer}textarea{display:block;width:100%;min-height:85px;font:13px/1.5 monospace;padding:12px;margin:12px 0}code{font-size:12px;overflow-wrap:anywhere}input{accent-color:#294f54}
</style>'''
    page += '<header><h1>Photography · 语义搜索</h1><p class="summary">' + html.escape(result["query"]) + '</p>'
    page += f'<p>本次范围 {coverage["total"]} 张 · 可搜索 {coverage["ready"]} 张 · 缺少有效分析 {coverage["needs_analysis"]} 张 · 待建向量 {coverage["missing"]} 张 · 过期 {coverage["stale"]} 张 · 无效 {coverage["invalid"]} 张</p>'
    page += '<p class="muted">已保存搜索快照 · 分数和覆盖率来自快照生成时，不代表当前索引状态。相似度不是符合条件的概率；查看快照不会改动相册。</p></header><main>'
    page += ''.join(cards) or '<p>此快照没有搜索结果。新的本地索引与语义查询尚未实现。</p>'
    page += '''</main><footer><h2>选择结果加入相册</h2><p>勾选照片后复制下方标识，并告诉 Skill 要加入的相册。也可下载选择文件。页面不会直接修改数据库。</p><textarea id="selection" readonly aria-label="已选照片标识">[]</textarea><button id="download">下载选择文件</button><p class="muted">使用与本页面同时生成的搜索 JSON，保存相册时按这些照片标识操作，不重新搜索。</p></footer><script>
function selected(){return Array.from(document.querySelectorAll('input:checked')).map(i=>i.value)}
document.querySelectorAll('input').forEach(i=>i.addEventListener('change',()=>document.getElementById('selection').value=JSON.stringify(selected(),null,2)));
document.getElementById('download').addEventListener('click',()=>{const ids=selected();if(!ids.length)return;const blob=new Blob([JSON.stringify(ids,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='selected-photo-ids.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
</script></html>'''
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return str(target)
