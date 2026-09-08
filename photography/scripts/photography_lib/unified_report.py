"""User-only HTML for explicitly selected, current unified-search evidence."""
from __future__ import annotations

import html

from .exports import prepare_export, write_export
from .management_report import _preview


_STATUSES = {
    "matched": "已保存条件为真（true）",
    "not_matched": "已保存条件为假（false）",
    "eligible": "仅有相似度证据，未经视觉确认",
    "not_ranked": "未参与相似度排序（本地预筛选，不是语义为假）",
    "unknown_missing_index": "未知（unknown）：缺少索引",
    "unknown_stale": "未知（unknown）：索引已过期",
    "unknown_invalid": "未知（unknown）：索引无效",
    "unsupported": "未知（unknown）：此条件不受支持",
}
_FACTS = {
    "semantic": (
        ("candidate_rank", "相似度候选排名"),
        ("score_gap_from_best", "与最高相似度的差值"),
        ("score_gap_to_next", "与下一候选的差值"),
    ),
    "object_count": (("count", "保存的目标数量"),),
    "ocr_contains": (("snippet", "相关的已保存 OCR 摘录"),),
    "color_fraction": (("fraction", "保存的颜色比例"),),
    "subject_position": (("center_x", "主体横向中心"), ("center_y", "主体纵向中心")),
    "scene": (("score", "保存的场景相似度"),),
    "has_near_duplicate": (("best_distance", "最小重复距离"), ("peer_count", "相似照片数量")),
}


def _text(value):
    return html.escape(str(value), quote=True)


def _number(value):
    return format(value, ".6g") if type(value) is float else str(value)


def _condition_label(condition):
    kind = condition["kind"]
    if kind == "semantic":
        return "语义描述：" + condition["query"]
    if kind == "object_count":
        operator = {"eq": "=", "ge": "≥", "le": "≤"}[condition["operator"]]
        return (f"目标 {condition['class_id']} 数量 {operator} {condition['value']}"
                f"（保存的检测分数阈值 ≥ {_number(condition['score_threshold'])}）")
    if kind == "ocr_contains":
        return "OCR 包含文字：" + condition["text"]
    if kind == "color_fraction":
        return f"颜色 {condition['color']} 比例 ≥ {_number(condition['minimum'])}"
    if kind == "subject_position":
        axes = [condition[key] for key in ("horizontal", "vertical") if condition.get(key) is not None]
        return "主体位置：" + " / ".join(axes)
    if kind == "scene":
        return f"场景 {condition['scene_id']} 相似度 ≥ {_number(condition['minimum'])}"
    return f"存在近似重复照片：{condition['metric']} 距离 ≤ {condition['max_distance']}"


def _facts(condition, cell):
    entries = []
    if condition["kind"] == "semantic" and cell["raw_score"] is not None:
        entries.append(("保存的余弦相似度", cell["raw_score"]))
    entries.extend((label, cell["evidence"][key]) for key, label in _FACTS[condition["kind"]]
                   if key in cell["evidence"] and cell["evidence"][key] is not None)
    if not entries:
        return "<p>没有可用的已保存事实。</p>"
    return "<dl>" + "".join(
        "<dt>" + label + "</dt><dd>" + _text(_number(value)) + "</dd>"
        for label, value in entries) + "</dl>"


def _header(snapshot, selected_count):
    query = snapshot["query"]
    heading = "Smart Albums · 统一搜索筛选结果 · " + snapshot["album"]["name"]
    operator = "AND（全部条件）" if query["operator"] == "and" else "OR（任一条件）"
    result = "<header><h1>" + _text(heading) + "</h1><p>原始查询：" + _text(query["query"]) + "</p>"
    result += "<p>条件组合：" + operator + "</p>"
    for label, conditions in (
            ("查询条件与已保存索引覆盖", query["conditions"]),
            ("补充事实（不参与 AND / OR 筛选）", query.get("evidence_conditions", []))):
        if not conditions:
            continue
        result += "<h2>" + label + "</h2><ul>"
        for condition in conditions:
            coverage = "；".join(_STATUSES[status] + " " + str(count)
                                 for status, count in snapshot["coverage"][condition["id"]].items())
            result += ("<li><strong>" + _text(condition["id"]) + "</strong> · "
                       + _text(_condition_label(condition)) + "<p>" + _text(coverage or "范围内没有照片") + "</p></li>")
        result += "</ul>"
    result += "<h2>文字编码查询</h2>"
    if snapshot["query_encodings"]:
        result += "<ul>"
        for condition_id, plan in snapshot["query_encodings"].items():
            used = snapshot["coverage"][condition_id].get("eligible", 0) > 0
            label = "实际编码文本" if used else "保存的待编码文本（本次未执行文字编码）"
            for prompt in plan["prompts"]:
                result += "<li>" + _text(condition_id) + " · " + label + "：" + _text(prompt) + "</li>"
        result += "</ul>"
    else:
        result += "<p>本次只使用已保存的结构化事实，没有文字编码。</p>"
    scope = snapshot["scope"]
    if scope["kind"] == "album":
        result += "<p>查询范围：整个相册。</p>"
    else:
        operation = "并集" if scope["match"] == "union" else "交集"
        names = "、".join(folder["name"] for folder in scope["folders"])
        result += "<p>查询时文件夹范围（" + operation + "）：" + _text(names) + "</p>"
    result += ("<p>查询范围 " + str(snapshot["scope_total"]) + " 张 · 可送审候选 "
               + str(snapshot["candidate_total"]) + " 张 · 本次送审 " + str(snapshot["candidate_count"])
               + " 张 · AI 选择展示 " + str(selected_count) + " 张 · 未送审候选 "
               + str(snapshot["retrieval"]["unretrieved_candidate_count"]) + " 张。</p>")
    result += "<p>部分覆盖：" + ("是" if snapshot["partial"] else "否") + "。"
    if snapshot["partial"]:
        result += "候选数量限制使本次送审仅覆盖一部分可送审候选。"
    result += "缺少、过期或无效索引所造成的未知不等于 false；未送审或未选择的照片不能据此断言不匹配。</p>"
    result += (
        '<p class="notice">AI 基于已保存证据进行整体筛选，不是看图核对或视觉确定性结论。'
        "相似度不是概率，候选排名不保证符合描述；不同文字查询的分数不能当作同一个量比较。"
        "结构化条件的 true / false 只描述保存的索引事实，unknown 表示未知。</p>"
        "<p>只读历史快照；范围、文件夹名称、数量和证据以查询时为准，后续文件夹变化不触发重新搜索。"
        "本次未检查原图，不运行图片模型。页面仅供用户查看，预览不提供给 AI；"
        "只嵌入与所选照片身份及哈希一致的已保存预览，不可用时不替换图片。"
        "本页面没有网络请求、脚本或选择表单，不修改相册或索引。</p></header>")
    return result


def unified_report(snapshot, output, *, config, store) -> str:
    from .unified_search import selected_rows

    target = prepare_export(output, config, store, (".html",))
    with store.read_snapshot():
        rows = selected_rows(snapshot, store=store)
        header = _header(snapshot, len(rows))
        cards = []
        conditions = snapshot["query"]["conditions"]
        supplemental = snapshot["query"].get("evidence_conditions", [])
        supplemental_ids = {condition["id"] for condition in supplemental}
        for row in rows:
            evidence = ""
            for condition in conditions + supplemental:
                cell = row["conditions"][condition["id"]]
                label = "补充事实 · " if condition["id"] in supplemental_ids else ""
                evidence += ("<section><h3>" + _text(condition["id"]) + " · "
                             + label + _text(_condition_label(condition)) + "</h3><p>"
                             + _STATUSES[cell["status"]] + "</p>" + _facts(condition, cell) + "</section>")
            cards.append("<article>" + _preview(row, store) + '<div class="body"><h2>照片编号 '
                         + str(row["number"]) + "</h2>" + evidence + "</div></article>")
    empty = ('<p class="empty">本次明确选择了 0 张照片（[]），没有可展示的所选照片。'
             "这不是待筛选候选列表，也不表示整个相册没有匹配照片；请结合上述索引覆盖和送审范围判断。</p>")
    document = _DOCUMENT + header + "<main>" + ("".join(cards) or empty) + "</main></body></html>"
    write_export(target, document.encode("utf-8"))
    return str(target)


_DOCUMENT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Smart Albums · 统一搜索筛选结果</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.6 system-ui}
header,main{max-width:1280px;margin:auto;padding:24px}h1{font-size:28px}
h2{font-size:20px}h3{font-size:16px}h1,h2,h3,p,li,dd{overflow-wrap:anywhere}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}
article{background:white;border:1px solid #dce1d9;border-radius:12px;overflow:hidden}
img{width:100%;height:300px;object-fit:contain;background:#e6e9e2}
.body,.preview-error{padding:20px}.preview-error{color:#8a3b20}
section{border-top:1px solid #dce1d9}dl{margin:8px 0}dt{font-weight:600}dd{margin:0 0 8px}
.notice{border-left:4px solid #406b67;padding-left:16px}.empty{grid-column:1/-1}
</style></head><body>"""
