"""Escaped, offline proposal/progress snapshot; opening it never executes work."""
import html
import json
from .exports import export_path
from .config import PhotographyError


def workflow_report(plan, output, *, config, store):
    output = export_path(output, config, store, (".html",))
    esc = lambda value: html.escape(str(value))
    execution = plan["execution"]
    counts = plan.get("summary", {}).get("counts") or {k: plan[k] for k in ("cached", "pending", "failed")}
    labels = {"cached": "可直接复用", "pending": "待处理", "failed": "无法完成", "analyzed": "已完成",
        "stale": "输入已变化", "running": "处理中", "submitted": "等待远程结果", "unknown": "需要核对", "cancelled": "已取消"}
    reasons = {"cache_match": "已有同配置分析", "group_cache_match": "已有相同多图组合分析", "never_analyzed": "尚未分析",
        "configuration_changed": "已有分析的模型或配置不同", "input_changed": "照片或缩略图已变化", "forced": "本次要求重新分析"}
    states = {"proposed": "等待确认", "confirmed": "已确认", "running": "执行中", "submitted": "已提交，等待回收",
        "paused": "已暂停，可恢复", "attention": "需要核对", "completed": "完成", "partial": "部分完成"}
    messages = {
        "Input tokens, cost and token-based throughput are unknown. Set an estimate from the selected model's metering rules; JPEG bytes are not tokens.": "尚无可靠的输入 token 估算，因此费用和处理时间显示为未知；图片文件大小不能直接换算 token。",
        "Experimental multi-image recipe: quality unverified, usage is per request, and caches are specific to the complete image group.": "多图为实验选项，效果尚未实测。用量按整个请求记录，缓存与整组图片关联。",
        "Concurrency reduced to one: this channel or unknown request/token limits require conservative execution.": "当前渠道或账号限额尚不明确，建议先以一个并发请求执行。",
        "Batch uploads a file of JPEG previews and may take up to the provider's completion window. File cleanup is explicit after collection.": "Batch 会上传包含缩略图的批处理文件，并异步返回结果；回收结果后可清理远程文件。",
        "Flexible wait and substantial remaining work favor Batch.": "待处理量较大，且允许等待，推荐异步 Batch。",
        "Immediate results or small/unknown workload favor immediate processing.": "需要即时结果，或工作量较小/信息不足，推荐即时处理。",
    }
    cards = ''.join(f'<div class="card"><strong>{esc(value)}</strong><span>{esc(labels[key])}</span></div>' for key, value in counts.items())
    rows = []
    for entry in plan.get("results", plan["snapshots"]):
        try:
            name = store.photo(entry["photo_id"])["relative_path"]
        except PhotographyError:
            name = entry["photo_id"]
        reason = entry.get("reason", entry.get("error",{}).get("message",""))
        rows.append(f'<tr><td>{esc(name)}</td><td>{esc(labels.get(entry["status"],entry["status"]))}</td><td>{esc(reasons.get(reason, reason))}</td></tr>')
    mode = "即时处理" if execution["mode"] == "immediate" else "异步 Batch"
    explanation = (f'每请求 {execution["images_per_request"]} 张 · 并发 {execution["concurrency"]}' if execution["mode"] == "immediate"
                   else "每个独立请求 1 张 · 上传后异步处理，结果需要回收")
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Albums · 分析方案</title><style>
*{box-sizing:border-box}body{margin:0;background:#f6f4ef;color:#233e42;font:16px/1.7 system-ui}
main{max-width:1100px;margin:auto;padding:40px 24px}h1{margin:0;font-size:32px}.muted{color:#60777a}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.card{background:white;border-radius:12px;padding:16px 24px;min-width:130px}
strong{display:block;font-size:32px}section{padding:24px;border-radius:12px;background:white;margin:20px 0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #e3e8e2;padding:10px;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}li{margin:8px 0}
</style><main><h1>Smart Albums · 分析方案</h1><p class="muted">本地快照。打开此页面不会上传照片或调用模型。</p>'''
    page += f'<p>方案状态：{esc(states.get(plan["status"], plan["status"]))} · 选中 {plan["requested"]} 张</p><div class="cards">{cards}</div>'
    channel = "OpenAI API" if plan["settings"]["provider"] == "openai" else "Codex 登录"
    page += f'<section><h2>{mode}</h2><p>渠道：{channel} · 模型：{esc(plan["settings"]["model"])}</p><p>{explanation}</p>'
    page += f'<p>初始请求：{execution["initial_requests"]} 次 · 每请求最多尝试：{execution["max_attempts_per_request"]} 次</p>'
    cost = execution["estimates"]["cost"]
    page += '<p>费用：' + (esc(cost["range"]) + ' USD（估算，非账单）' if cost else '未知；没有足够依据时不显示精确报价。') + '</p>'
    page += '<ul>' + ''.join('<li>' + esc(messages.get(v, v)) + '</li>' for v in execution["reasons"] + execution["warnings"]) + '</ul></section>'
    if plan.get("summary"):
        page += '<section><h2>执行与用量</h2><pre>' + esc(json.dumps(plan["summary"], ensure_ascii=False, indent=2)) + '</pre></section>'
    page += '<section><h2>照片范围</h2><table><thead><tr><th>照片</th><th>状态</th><th>原因</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></section>'
    page += '<details><summary>完整方案与确认摘要</summary><pre>' + esc(json.dumps(plan, ensure_ascii=False, indent=2)) + '</pre></details></main></html>'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return str(output)
