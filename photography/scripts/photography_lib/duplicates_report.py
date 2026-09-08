"""Complete, static Chinese duplicate reports; no external resources or original reads."""
from __future__ import annotations

import html

from . import duplicates
from .exports import prepare_export, write_export
from .report_preview import snapshot_preview
from .source_paths import photo_filename


def _text(value):
    return html.escape(str(value) if value is not None else "未知", quote=True)


def _size(value):
    if type(value) is not int or value < 0:
        return "未知"
    return f"{value / 1024 / 1024:.2f} MB" if value >= 1024 * 1024 else f"{value / 1024:.1f} KB"


def duplicate_report(snapshot, output, *, config, store):
    target = prepare_export(output, config, store, (".html",))
    rows = duplicates.validate(snapshot, store=store)
    labels = {"exact": "完全重复", "similar": "疑似重复", "mixed": "完全重复与疑似重复"}
    numbers = {pid: f"{group['number']}.{index}" for group in snapshot["groups"]
               for index, pid in enumerate(group["member_ids"], 1)}
    sections = []
    with store.read_snapshot():
        for group in snapshot["groups"]:
            exact = {pid: index for index, members in enumerate(group["exact_subgroups"], 1) for pid in members}
            cards = []
            for pid in group["member_ids"]:
                row = rows[pid]
                metadata, match = row["metadata"], row["match"]
                width = metadata.get("display_width") or metadata.get("width")
                height = metadata.get("display_height") or metadata.get("height")
                dimensions = f"{width} × {height}" if width and height else "未知"
                best = match["best_match"]
                reason = ("文件 SHA-256 相同" if best["metric"] == "exact" else f"感知哈希距离 {best['distance']}")
                subgroup = f'<span class="badge">完全重复子组 {exact[pid]}</span>' if pid in exact else ""
                path = row.get("original_absolute_path") or row.get("original_relative_path")
                cards.append('<article data-photo-id="' + _text(pid) + '">' + snapshot_preview(row, store)
                             + '<div class="card-body"><h3>' + _text(numbers[pid]) + " · " + _text(photo_filename(row))
                             + "</h3>" + subgroup + '<p class="path">' + _text(path) + "</p>"
                             + "<dl><dt>原图尺寸</dt><dd>" + _text(dimensions) + "</dd><dt>文件大小</dt><dd>"
                             + _size(row.get("size_bytes")) + "</dd><dt>拍摄时间</dt><dd>"
                             + _text(metadata.get("datetime_original")) + "</dd></dl><p class=reason>"
                             + _text(reason) + " · 配对 " + _text(numbers[best["photo_id"]]) + "</p><p>直接匹配 "
                             + str(match["exact_peer_count"] + match["similar_peer_count"]) + " 张</p></div></article>")
            sections.append('<section><div class="group-heading"><h2>第 ' + str(group["number"]) + " 组 · "
                            + labels[group["kind"]] + "</h2><span>" + str(group["member_count"]) + " 张照片 · "
                            + str(group["pair_counts"]["total"]) + " 对直接匹配</span></div>"
                            + ("<p>同组不代表任意两张都匹配；请按卡片中的直接配对核对。</p>" if group["kind"] != "exact" else "")
                            + '<div class="cards">' + "".join(cards) + "</div></section>")
    scope = snapshot["scope"]
    scope_text = ("整个相册" if scope["kind"] == "album" else "指定照片" if scope["kind"] == "photo_ids" else
                  "、".join(folder["name"] for folder in scope["folders"]) + ("（并集）" if scope["match"] == "union" else "（交集）"))
    coverage = []
    state_labels = {"missing": "缺少指纹", "stale": "指纹过期", "invalid_input": "输入无效",
                    "invalid_result": "指纹损坏", "dependency_missing": "缺少依赖",
                    "incomplete_result": "指纹不完整", "not_configured": "未配置感知哈希"}
    for branch, entry in snapshot["coverage"].items():
        if entry["requested"]:
            reasons = "、".join(f"{state_labels.get(state, state)} {count} 张" for state, count in entry["counts"].items()
                               if state != "eligible")
            coverage.append("<li>" + labels[branch] + "：已检查 " + str(entry["eligible"]) + " / " + str(entry["total"])
                            + " 张" + ("；未检查原因 " + _text(reasons) if reasons else "") + "</li>")
    complete_text = ("本次规则的扫描覆盖完整" if snapshot["complete"] else "检查不完整：部分照片尚未参与请求的比较")
    empty = ("在本次规则下未发现重复候选。" if snapshot["complete"] else "在已检查照片中未发现重复候选；未检查部分仍可能存在重复。")
    stats = snapshot["summary"]
    mode = snapshot["parameters"]["mode"]
    title = "Smart Albums · 照片查重"
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Smart Albums · 照片查重</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.6 system-ui}
header,main,footer{max-width:1400px;margin:auto;padding:24px}h1{margin:0;font-size:30px}h2{font-size:21px}
h3{font-size:16px;margin:0;overflow-wrap:anywhere}.eyebrow{letter-spacing:.14em;font-size:12px;color:#617875}
.stats{font-size:23px;font-weight:600}.coverage{padding:16px 22px;background:#e7ebe2;border-radius:12px}
section{margin-bottom:40px}.group-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:18px}
article{background:white;border:1px solid #dce1d9;border-radius:12px;overflow:hidden}
img{width:100%;height:270px;object-fit:contain;background:#e6e9e2}.card-body{padding:20px}
.path{font-size:12px;color:#637772;overflow-wrap:anywhere}.badge{display:inline-block;margin-top:10px;padding:2px 9px;background:#e2eee7;border-radius:20px;font-size:12px}
dl{display:grid;grid-template-columns:85px 1fr;font-size:13px;gap:4px}dt{color:#637772}dd{margin:0;overflow-wrap:anywhere}
.reason{font-weight:600;font-size:14px}.preview-error{min-height:270px;padding:24px;color:#8a3b20;overflow-wrap:anywhere}
footer{font-size:12px;color:#637772;overflow-wrap:anywhere}.empty{padding:32px;background:white;border-radius:12px}
</style></head><body>'''
    page += '<header><p class="eyebrow">SMART ALBUMS / DUPLICATES</p><h1>' + title + "</h1><p>"
    page += _text(snapshot["album"].get("name")) + " · " + _text(scope_text) + " · " + _text(snapshot["created_at"]) + "</p>"
    page += '<p class="stats">' + str(stats["group_count"]) + " 组 · " + str(stats["matched_photo_count"]) + " 张照片 · " + str(stats["pair_count"]) + " 对直接匹配</p>"
    page += '<div class="coverage"><strong>' + complete_text + "</strong><ul>" + "".join(coverage) + "</ul>"
    if mode != "exact":
        page += "<p>感知哈希阈值：" + str(snapshot["parameters"]["max_distance"]) + "。距离越小越接近，不代表重复概率。</p>"
    page += "</div></header><main>" + ("".join(sections) or '<p class="empty">' + empty + "</p>") + "</main>"
    page += "<footer>历史快照 · " + _text(snapshot["snapshot_id"]) + "<p>结果基于最近导入的数据，本次未检查原图。照片编号属于此快照；后续相册变化不会更新这些结果。</p></footer></body></html>"
    write_export(target, page.encode("utf-8"))
    return str(target)
