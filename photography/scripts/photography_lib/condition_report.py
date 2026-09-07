"""Standalone user-only pages of already validated, finalized condition results."""
from __future__ import annotations

import html
import json
from uuid import UUID

from .config import PhotographyError
from .exports import prepare_export, write_export
from .management import snapshot_scope
from .management_report import _preview


def _text(value):
    return html.escape(str(value), quote=True)


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _validate_page(page, store):
    if not isinstance(page, dict) or page.get("schema") != "condition-search-page-v1":
        raise PhotographyError("INVALID_ARGUMENT", "Render a validated condition-search-page-v1 result page.")
    try:
        album_id = UUID(page["album"]["id"])
        json.dumps(page, allow_nan=False)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PhotographyError("INVALID_ARGUMENT", "Result pages require an album UUID and finite JSON.") from exc
    if album_id != UUID(store.album()["id"]):
        raise PhotographyError("ALBUM_MISMATCH", "This result page belongs to another album; no previews were read.")
    snapshot_scope(page)
    if (not isinstance(page.get("conditions"), list) or not page["conditions"]
            or any(not isinstance(condition, dict) or not isinstance(condition.get("id"), str)
                   for condition in page["conditions"])
            or not isinstance(page.get("results"), list)
            or any(not isinstance(page.get(key), dict) for key in ("aliases", "coverage", "evaluated_coverage", "retrieval"))
            or any(type(page.get(key)) is not int or page[key] < 0
                   for key in ("scope_total", "candidate_count", "total"))):
        raise PhotographyError("INVALID_ARGUMENT", "Result page conditions, coverage and counts are required.")
    condition_ids = [condition["id"] for condition in page["conditions"]]
    if len(set(condition_ids)) != len(condition_ids) or not (
            len(page["results"]) <= page["total"] <= page["candidate_count"] <= page["scope_total"]):
        raise PhotographyError("INVALID_ARGUMENT", "Invalid result page conditions or counts.")
    previous_rank = None
    photo_ids = set()
    from .condition_queries import validate_cell

    for row in page["results"]:
        if (not isinstance(row, dict) or not isinstance(row.get("photo_id"), str)
                or row["photo_id"] in photo_ids or type(row.get("result_rank")) is not int
                or not 1 <= row["result_rank"] <= page["total"]
                or previous_rank is not None and row["result_rank"] != previous_rank + 1
                or not isinstance(row.get("conditions"), dict) or set(row["conditions"]) != set(condition_ids)
                or not isinstance(row.get("matched_condition_ids"), list)
                or any(not isinstance(value, str) for value in row["matched_condition_ids"])
                or type(row.get("matched_count")) is not int or row["matched_count"] < 1
                or type(row.get("pattern_score")) not in (int, float) or not 0 <= row["pattern_score"] <= 1):
            raise PhotographyError("INVALID_ARGUMENT", "Invalid finalized result row.")
        matched = []
        for condition in page["conditions"]:
            cell = validate_cell(condition, row["conditions"][condition["id"]], finalized=True)
            if cell["status"] == "not_reviewed":
                raise PhotographyError("QUERY_STAGE_INVALID", "Complete semantic review before rendering results.")
            if cell["status"] == "matched":
                matched.append(condition["id"])
        if (set(row["matched_condition_ids"]) != set(matched) or row["matched_count"] != len(matched)
                or len(row["matched_condition_ids"]) != len(matched)):
            raise PhotographyError("INVALID_ARGUMENT", "Result match counts disagree with the condition cells.")
        photo_ids.add(row["photo_id"])
        previous_rank = row["result_rank"]


def condition_report(page, output, *, config, store):
    target = prepare_export(output, config, store, (".html",))
    cards = []
    with store.read_snapshot():
        _validate_page(page, store)
        for row in page["results"]:
            cells = "".join(
                "<tr><th>" + _text(condition["id"]) + "</th><td>"
                + _text(row["conditions"][condition["id"]]["status"]) + "</td><td>"
                + _text(row["conditions"][condition["id"]].get("normalized_score")) + "</td><td><pre>"
                + _json(row["conditions"][condition["id"]]) + "</pre></td></tr>"
                for condition in page["conditions"])
            cards.append(
                "<article>" + _preview(row, store) + '<div class="body"><h2>全局排名 '
                + _text(row["result_rank"]) + " · " + _text(row["photo_id"]) + "</h2><p>命中 "
                + _text(row["matched_count"]) + " 个条件 · 条件集合 "
                + _text(", ".join(row["matched_condition_ids"])) + " · 同集合分数 "
                + _text(row["pattern_score"]) + "</p><table><thead><tr><th>条件</th><th>状态</th>"
                "<th>归一化分数</th><th>已保存证据</th></tr></thead><tbody>" + cells
                + "</tbody></table></div></article>")
    heading = "Smart Albums · OR 多条件结果 · " + str(page["album"].get("name", ""))
    header = "<header><h1>" + _text(heading) + "</h1><p>相册：" + _json(page["album"]) + "</p>"
    header += "<p>只读历史快照 · " + _text(page.get("created_at", "")) + "</p>"
    header += ("<p>OR：至少命中一个条件才进入结果；先按命中条件数降序。unknown 表示未知，不是未命中；"
               "not_reviewed 只在查询时输入覆盖率中表示语义索引可用，不表示已经匹配。"
               "所有候选的可评审语义条件都完成评审后，才计算命中数和排名。余弦不是概率，top-K 不是匹配保证。</p>"
               "<p>分数只能比较相同命中条件 ID 集合：exact 命中为 1；graded 在同条件命中总体中按方向计算"
               "平均名次百分位，平分取平均，单项或全相等为 1；同集合等权平均。相同命中数的不同条件组合"
               "按冻结随机种子交织，不能跨组合比较分数。分页保留全局排名，不从 1 重新计数。</p>")
    header += "<p>查询范围 " + _text(page["scope_total"]) + " · 候选池 " + _text(page["candidate_count"])
    header += " · 最终命中 " + _text(page["total"]) + " · 本页 " + str(len(page["results"])) + "</p>"
    header += ("<p>语义候选上限可能造成部分覆盖；未取回的照片未经评审，不能断言不匹配。"
               "范围、文件夹名称、覆盖率与证据都是查询时历史记录，文件夹后续变化不触发重新查询。"
               "本次未检查原图；只展示身份与哈希匹配的保存预览，无法匹配时显示错误，不替换图片。"
               "页面仅供用户查看，不向 agent 提供图片；没有选择控件、表单、脚本或后端，不修改相册或索引。</p>")
    for label, key in (
            ("冻结条件", "conditions"), ("重复条件别名", "aliases"), ("历史查询范围", "scope"),
            ("实际使用的文字查询", "query_encodings"),
            ("输入资格覆盖率（整个查询范围）", "coverage"), ("最终评估覆盖率（候选池）", "evaluated_coverage"),
            ("候选上限与部分覆盖", "retrieval"), ("冻结随机种子", "random_seed")):
        header += "<h2>" + label + "</h2><pre>" + _json(page.get(key)) + "</pre>"
    header += "</header>"
    document = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Smart Albums · OR 多条件结果</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.6 system-ui}
header,main,footer{max-width:1280px;margin:auto;padding:24px}h1{font-size:28px}h2{overflow-wrap:anywhere}
article{background:white;border:1px solid #dce1d9;border-radius:12px;margin-bottom:24px;overflow:hidden}
img{width:100%;height:320px;object-fit:contain;background:#e6e9e2}.body,.preview-error{padding:20px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}table{width:100%;table-layout:fixed;border-collapse:collapse}
th,td{text-align:left;vertical-align:top;border:1px solid #dce1d9;padding:8px;overflow-wrap:anywhere}
</style></head><body>"""
    document += header + "<main>" + ("".join(cards) or "<p>此页没有匹配结果；请结合缺失索引与部分覆盖判断。</p>") + "</main>"
    document += "<footer><p>下一页游标：" + _text(page.get("next_cursor")) + "</p></footer></body></html>"
    write_export(target, document.encode("utf-8"))
    return str(target)
