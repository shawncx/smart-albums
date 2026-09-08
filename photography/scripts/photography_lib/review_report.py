"""Self-contained, local review reports with measured rather than inferred usage."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .config import PhotographyError
from .exports import prepare_export, write_export
from .management_report import _preview
from .review import job
from .review_schema import DIMENSIONS, dimension_scores
from .source_paths import photo_filename


def _text(value):
    return html.escape(str(value), quote=True)


def _list(values):
    return "<ul>" + "".join("<li>" + _text(value) + "</li>" for value in values) + "</ul>"


def _improvements(payload):
    if payload["schema_version"] == "photo-review-v1":
        return _list(payload["improvements"])
    entries = []
    for item in payload["improvements"]:
        entry = ("<li><strong>" + _text(item["kind"]) + ": " + _text(item["action"])
                 + "</strong><p>" + _text(item["rationale"]) + "</p>")
        if item["tradeoff"] is not None:
            entry += "<p>Trade-off: " + _text(item["tradeoff"]) + "</p>"
        entries.append(entry + "</li>")
    return "<ul>" + "".join(entries) + "</ul>"


def _metric(metadata, key, suffix=""):
    value = metadata.get(key)
    if value is None:
        return "Not reported"
    return _text(f"{value:.3f}" if isinstance(value, float) else value) + suffix


def _usage(batch, metadata):
    scope = "Per-photo measurement (one image in the request)" if len(batch["items"]) == 1 else (
        f"Shared batch measurement ({len(batch['items'])} images); NOT per-photo usage")
    body = '<h3>Measured usage</h3><p>' + _text(scope) + '</p><dl class="usage">'
    for label, key, suffix in (
        ("Active batch time", "elapsed_seconds", " s"), ("Input tokens", "input_tokens", ""),
        ("Output tokens", "output_tokens", ""), ("Cache-read tokens", "cache_read_tokens", ""),
        ("Cache-write tokens", "cache_write_tokens", ""),
        ("Reasoning tokens (included in output)", "reasoning_tokens", ""),
        ("Provider credits", "credits", " " + _text(metadata.get("credits_unit", ""))),
    ):
        body += "<dt>" + label + "</dt><dd>" + _metric(metadata, key, suffix) + "</dd>"
    body += "</dl>"
    if batch["attempts"] > 1:
        body += "<p>Latest attempt only; earlier attempt usage may be unavailable.</p>"
    if metadata.get("usage_source"):
        body += "<p>Usage source: " + _text(metadata["usage_source"]) + "</p>"
    return body


def _attempts(batches, approvals):
    known = {}
    for approval in approvals:
        for attempt in approval.get("previous_attempts", []):
            known[(attempt["batch_id"], attempt["attempts"])] = attempt
    for batch in batches:
        if batch["attempts"]:
            known[(batch["batch_id"], batch["attempts"])] = batch
    return list(known.values())


def _observed(batches, key, approvals):
    attempted = _attempts(batches, approvals)
    values = [batch["metadata"][key] for batch in attempted if key in batch["metadata"]]
    if not values:
        return "Not reported"
    total = sum(values)
    label = str(total)
    expected = sum(batch["attempts"] for batch in batches)
    if len(values) != expected:
        label += " observed (incomplete attempt coverage)"
    return _text(label)


def review_report(run_id, output, *, store, config):
    if not Path(output).is_absolute():
        raise PhotographyError("INVALID_ARGUMENT", "Review report requires an absolute HTML output path.")
    target = prepare_export(output, config, store, (".html",))
    if target.replace_existing:
        raise PhotographyError("EXPORT_EXISTS", "Choose an unused report destination; existing reports are not overwritten.")
    cards = []
    with store.read_snapshot():
        saved = job(store, run_id)
        records = {record["photo_id"]: record for record in saved["results"]}
        batches = {batch["batch_id"]: batch for batch in saved["batches"]}
        for ordinal, item in enumerate(saved["items"], 1):
            photo_id = item["photo_id"]
            photo = store.photo(photo_id)
            heading = f"{ordinal:02d} / " + photo_filename(photo)
            picture = _preview({"photo_id": photo_id, **item["input_manifest"]}, store)
            record = records.get(photo_id)
            body = '<p class="identity">' + _text(photo_id) + "</p><h2>" + _text(heading) + "</h2>"
            if record is None:
                batch = next((value for value in batches.values()
                              if any(entry["photo_id"] == photo_id for entry in value["items"])), None)
                state = batch["status"] if batch else saved["status"]
                body += '<p class="notice">No successful review saved. Status: ' + _text(state) + "</p>"
                if batch and batch["error"]:
                    body += "<p>" + _text(batch["error"]["code"]) + ": " + _text(batch["error"]["message"]) + "</p>"
                if batch and batch["attempts"]:
                    body += _usage(batch, batch["metadata"])
            else:
                payload = record["payload"]
                body += '<p>Review status: ' + _text(payload.get("review_status", "reviewed")) + "</p>"
                if payload["overall_score"] is not None:
                    body += ('<p class="overall">' + _text(payload["overall_score"])
                             + '<small> / 10 · Application mean</small></p>')
                else:
                    body += '<p class="notice">Overall score unavailable: incomplete dimension evidence.</p>'
                body += '<p class="description">' + _text(payload["description"]) + "</p><div class=\"scores\">"
                for dimension in DIMENSIONS:
                    score = dimension_scores(payload)[dimension]
                    body += '<div class="dimension"><strong>' + _text(dimension.title()) + "</strong>"
                    if score["score"] is None:
                        body += '<span>Not assessable</span>'
                    else:
                        body += ('<span>' + _text(score["score"]) + ' / 10</span>'
                                 '<meter min="0" max="10" value="' + _text(score["score"]) + '"></meter>')
                    body += "<p>" + _text(score["reason"]) + "</p></div>"
                body += "</div><h3>Strengths</h3>" + _list(payload["strengths"])
                body += "<h3>Improvements</h3>" + _improvements(payload)
                body += '<div class="notice"><h3>Limitations</h3>' + _list(payload["limitations"]) + "</div>"
                batch = batches.get(record["batch_id"])
                if batch is None:
                    body += "<p>Cached review: no new Copilot request for this photo in this task.</p>"
                else:
                    body += _usage(batch, record["metadata"])
                body += "<details><summary>Structured result and provenance</summary><pre>" + _text(
                    json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)) + "</pre></details>"
            cards.append("<article>" + picture + '<div class="body">' + body + "</div></article>")
        batch_list = list(batches.values())
        active_time = f"{saved['elapsed_seconds']:.3f} s" if saved["status"] != "planned" else "Not executed"
        header = (
            '<header><p class="eyebrow">SMART ALBUMS / PHOTOGRAPHY CRITIQUE</p><h1>AI Review</h1>'
            "<p>" + _text(saved["album"]["name"]) + " &middot; " + _text(saved["profile"]["model"])
            + " &middot; " + _text(saved["status"]) + "</p>"
            '<div class="summary"><div><strong>' + str(len(records)) + " / " + str(len(saved["items"]))
            + "</strong><span>Reviews saved</span></div><div><strong>" + active_time
            + "</strong><span>Total active execution time</span></div><div><strong>"
            + _observed(batch_list, "input_tokens", saved["approval_history"]) + "</strong><span>Input tokens observed</span></div>"
            "<div><strong>" + _observed(batch_list, "output_tokens", saved["approval_history"])
            + "</strong><span>Output tokens observed</span></div></div>"
            '<p class="notice">Only provider-reported token/credit fields are shown. Missing usage is not zero. '
            "Copilot credits are not Azure credits; no Azure price or per-photo cost is inferred. "
            "Shared batch usage is never counted once per image. Retry totals may be incomplete. "
            "Active time excludes user confirmation waits and report generation.</p>"
            "<p>Each score is subjective, based on the stored preview. The overall score is the equal-weight "
            "six-dimension mean computed by the application only when all six scores are numeric. "
            "Structural validation does not verify visual evidence, language or completeness of preview limitations. "
            "This local snapshot makes no network requests and does not change the album.</p>"
            "<details><summary>Task provenance</summary><pre>" + _text(json.dumps({
                "run_id": run_id, "album_id": saved["album_id"], "created_at": saved["created_at"],
                "profile_id": saved["profile_id"], "rubric_version": saved["profile"]["rubric_version"],
                "batch_size": saved["batch_size"], "request_attempts": saved["request_attempts"],
                "digest": saved["digest"], "summary": saved["summary"],
            }, ensure_ascii=False, indent=2)) + "</pre></details></header>"
        )
        attempts = _attempts(batch_list, saved["approval_history"])
        if attempts:
            rows = []
            for attempt in sorted(attempts, key=lambda item: (item["ordinal"], item["attempts"])):
                metadata = attempt["metadata"]
                rows.append("<tr><td>" + str(attempt["ordinal"] + 1) + "</td><td>" + str(attempt["attempts"])
                            + "</td><td>" + _text(attempt["status"]) + "</td><td>"
                            + _metric(metadata, "elapsed_seconds", " s") + "</td><td>"
                            + _metric(metadata, "input_tokens") + "</td><td>"
                            + _metric(metadata, "output_tokens") + "</td></tr>")
            header += ('<section class="attempts"><h2>Batch attempt history</h2><p>Includes retained failed '
                       'attempts; retries can consume additional tokens.</p><div class="table-scroll"><table>'
                       '<thead><tr><th>Batch</th><th>Attempt</th><th>Status</th><th>Time</th>'
                       '<th>Input tokens</th><th>Output tokens</th></tr></thead><tbody>'
                       + "".join(rows) + "</tbody></table></div></section>")
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Smart Albums - AI Review</title><style>
:root{color-scheme:light;--ink:#18252e;--muted:#596c76;--accent:#176761;--border:#dce6e7}
*{box-sizing:border-box}body{margin:0;background:#eef3f2;color:var(--ink);font:16px/1.65 system-ui,sans-serif}
header,main{max-width:1200px;margin:auto;padding:36px 24px}header{padding-top:54px}h1{font-size:48px;line-height:1.1;margin:12px 0}
h2{font-size:22px;overflow-wrap:anywhere}h3{font-size:15px;margin-bottom:4px}p{margin:8px 0 16px}
.eyebrow,.identity{font-size:12px;letter-spacing:.12em;color:var(--muted)}.identity{overflow-wrap:anywhere}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:24px 0}
.summary>div{background:white;border:1px solid var(--border);border-radius:12px;padding:18px}
.summary strong{display:block;font-size:23px;color:var(--accent)}.summary span{font-size:13px;color:var(--muted)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:24px;padding-top:0}
article{background:white;border:1px solid var(--border);border-radius:18px;overflow:hidden;align-self:start}
article>img{display:block;width:100%;height:340px;object-fit:contain;background:#152028}.body{padding:26px}
.overall{font-size:36px;color:var(--accent);font-weight:700}.overall small{font-size:16px;font-weight:400}
.scores{display:grid;grid-template-columns:1fr 1fr;gap:14px}.dimension{padding:12px;background:#f3f7f7;border-radius:8px}
.dimension strong,.dimension span{display:block;font-size:13px}.dimension p{font-size:13px;margin:5px 0}
meter{width:100%;height:12px}.notice{border-left:3px solid #bd9949;padding:10px 15px;background:#fcf8ed;font-size:14px}
.usage{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:13px}.usage dd{margin:0;text-align:right}
details{margin-top:16px;font-size:13px}summary{cursor:pointer;color:var(--accent)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f7f7;padding:12px}
ul{padding-left:20px}.preview-error{padding:20px;color:#8d302c}footer{text-align:center;padding:20px;color:var(--muted)}
.attempts{max-width:1200px;margin:0 auto;padding:0 24px 30px}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;background:white}
td,th{text-align:left;padding:10px;border-bottom:1px solid var(--border);white-space:nowrap}
@media(max-width:540px){header,main{padding:24px 14px}.body{padding:18px}article>img{height:260px}.scores{grid-template-columns:1fr}}
</style></head><body>""" + header + "<main>" + "".join(cards) + "</main><footer>Local, read-only review snapshot</footer></body></html>"
    write_export(target, page.encode("utf-8"))
    return {"status": "written", "output": str(target), "run_id": run_id, "run_status": saved["status"],
            "photo_count": len(saved["items"]), "review_count": len(records), "provider_calls_this_operation": 0}
