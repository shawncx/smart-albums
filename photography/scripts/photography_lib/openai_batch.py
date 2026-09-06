"""Explicit asynchronous submission, reconciliation, collection and cleanup."""
import json
import re
import hashlib
from uuid import uuid4
from urllib.parse import quote
from .analysis_api import OpenAIHTTP, RequestFailure, build_payload, parse_results, token_usage
from .analysis_settings import make_provider
from .analysis_planner import require_confirmed
from .analysis_execution import prepare_request, start_attempt, fail_request, save_outputs, summarize
from .config import PhotographyError
from .sqlite_storage import now

REMOTE_DONE = {"completed", "failed", "expired", "cancelled"}


def remote_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise PhotographyError("INVALID_REMOTE_ID", "Invalid remote resource identifier.")
    return value


def multipart(content):
    boundary = "smartalbums" + uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="requests.jsonl"\r\n'
            'Content-Type: application/jsonl\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, "multipart/form-data; boundary=" + boundary


def save_job(store, plan, job):
    jobs = plan.setdefault("jobs", [])
    index = next((i for i, j in enumerate(jobs) if j["local_id"] == job["local_id"]), None)
    if index is None:
        jobs.append(job)
    else:
        jobs[index] = job
    store.save_plan(plan)


def submit_batches(plan, store, config, provider, http=None):
    http = http or OpenAIHTTP(provider)
    settings = plan["settings"]
    request_limit = settings["batch_max_requests"]
    if settings["queue_tokens"]:
        request_limit = min(request_limit, int(settings["queue_tokens"] // settings["input_tokens_per_photo"]))
    # Outstanding jobs count against the configured queue until collected.
    queue_used = sum(j.get("estimated_input_tokens", 0) for j in plan.get("jobs", []) if j["status"] not in ("collected", "rejected"))
    groups, current, length = [], [], 0
    try:
        provider.check_ready()
        for req in store.requests(plan["plan_id"]):
            if req["status"] != "pending":
                continue
            try:
                with store.transaction():
                    images = prepare_request(plan, req, store, config)
                if images is None:
                    continue
                line = (json.dumps(dict(custom_id=req["id"], method="POST", url="/v1/responses",
                     body=build_payload(provider.config, images)), ensure_ascii=False) + "\n").encode("utf-8")
                if len(line) > settings["batch_max_bytes"]:
                    raise PhotographyError("BATCH_ITEM_TOO_LARGE", "One request exceeds the configured batch file limit.")
                if request_limit < 1:
                    raise PhotographyError("BATCH_QUEUE_TOO_SMALL", "One image exceeds the configured queue budget.")
                if current and (length + len(line) > settings["batch_max_bytes"] or len(current) >= request_limit):
                    groups.append(current)
                    current, length = [], 0
                current.append((req, line))
                length += len(line)
            except PhotographyError as exc:
                with store.transaction():
                    fail_request(plan, req, store, exc.to_dict(), status="stale" if exc.code == "PHOTO_CHANGED" else "failed")
        if current:
            groups.append(current)
        for group in groups:
            estimate = len(group) * (settings["input_tokens_per_photo"] or 0)
            if settings["queue_tokens"] and (not estimate or estimate + queue_used > settings["queue_tokens"]):
                break
            content = b"".join(line for _, line in group)
            job = dict(local_id="batch_" + uuid4().hex, status="uploading", created_at=now(),
                request_ids=[r["id"] for r, _ in group], input_hash=hashlib.sha256(content).hexdigest(),
                estimated_input_tokens=estimate, input_bytes=len(content))
            with store.transaction():
                # Claims are acquired before either upload or task creation.
                for req, _ in group:
                    start_attempt(plan, req, store)
                    req["job_local_id"] = job["local_id"]
                    req["status"] = "submitting"
                    store.save_request(req)
                save_job(store, plan, job)
            try:
                body, content_type = multipart(content)
                uploaded = http.call("/files", method="POST", data=body, content_type=content_type)
                job["input_file_id"] = remote_id(uploaded.get("id"))
                job["status"] = "creating"
                with store.transaction():
                    save_job(store, plan, job)
                remote = http.call("/batches", method="POST", data=dict(input_file_id=job["input_file_id"],
                    endpoint="/v1/responses", completion_window="24h",
                    metadata={"smart_albums_plan": plan["plan_id"], "smart_albums_job": job["local_id"]}))
                job.update(remote_id=remote_id(remote.get("id")), status="submitted")
                queue_used += estimate
                with store.transaction():
                    save_job(store, plan, job)
                    for req, _ in group:
                        req["status"] = "submitted"
                        req["attempts"][-1]["status"] = "submitted"
                        store.save_request(req)
                        for snap in req["snapshots"]:
                            store.save_item(plan["plan_id"], {**snap, "status": "submitted", "request_id": req["id"]})
            except (PhotographyError, KeyError, TypeError) as exc:
                uncertain = not isinstance(exc, RequestFailure) or exc.uncertain
                job["status"] = "unknown" if uncertain else "rejected"
                error = exc.to_dict() if isinstance(exc, PhotographyError) else {"code": "SUBMISSION_UNKNOWN", "message": "Invalid submission response; reconcile before retrying."}
                job["error"] = error
                with store.transaction():
                    save_job(store, plan, job)
                    for req, _ in group:
                        req["attempts"][-1].update(status="unknown" if uncertain else "failed", error=error)
                        fail_request(plan, req, store, error, status="unknown" if uncertain else "failed")
                break
    except PhotographyError as exc:
        plan["execution_error"] = exc.to_dict()
    finally:
        with store.transaction():
            plan["status"] = "submitted" if any(j["status"] in ("submitted", "creating", "unknown") for j in plan.get("jobs", [])) else "paused"
            store.save_plan(plan)
    return summarize(store, plan["plan_id"])


def reconcile_job(plan, job, http):
    """Bounded read-only reconciliation. No absence claim or repeated creation."""
    after = ""
    matches = []
    for _ in range(10):
        page = http.call("/batches?limit=100" + ("&after=" + quote(after) if after else ""))
        for remote in page.get("data", []):
            if remote.get("metadata", {}).get("smart_albums_job") == job["local_id"] and remote.get("metadata", {}).get("smart_albums_plan") == plan["plan_id"]:
                matches.append(remote)
        if not page.get("has_more"):
            break
        after = remote_id(page.get("last_id"))
    if len(matches) == 1:
        job["remote_id"] = remote_id(matches[0]["id"])
        job["status"] = "submitted"
    else:
        job["reconciliation"] = "No unique matching job found; do not resubmit automatically. Check the provider dashboard."


def batch_action(plan_id, *, store, config, action="status", http=None):
    plan = store.plan(plan_id)
    require_confirmed(plan)
    if plan["execution"]["mode"] != "batch":
        raise PhotographyError("NOT_BATCH", "This is not an asynchronous Batch plan.")
    http = http or OpenAIHTTP(make_provider(plan["settings"]))
    for job in plan.get("jobs", []):
        if job["status"] in ("collected", "rejected") and action != "cleanup":
            continue
        if not job.get("remote_id") and job["status"] in ("unknown", "creating", "uploading"):
            reconcile_job(plan, job, http)
        if not job.get("remote_id"):
            if action == "cleanup" and job["status"] == "rejected" and job.get("input_file_id"):
                try:
                    http.call("/files/" + remote_id(job["input_file_id"]), method="DELETE")
                    job["deleted_files"] = [job["input_file_id"]]
                except PhotographyError as exc:
                    job["cleanup_error"] = exc.to_dict()
            with store.transaction():
                save_job(store, plan, job)
            continue
        rid = remote_id(job["remote_id"])
        remote = http.call("/batches/" + rid)
        job["remote_status"] = remote.get("status")
        if action == "cancel" and remote.get("status") not in REMOTE_DONE:
            remote = http.call("/batches/" + rid + "/cancel", method="POST")
            job["remote_status"] = remote.get("status")
        if action == "collect" and remote.get("status") in REMOTE_DONE:
            collect_job(plan, job, remote, store, config, http)
        if action == "cleanup":
            if job["status"] != "collected" and job["status"] != "rejected":
                raise PhotographyError("COLLECT_FIRST", "Collect results before removing remote files.")
            deleted = job.setdefault("deleted_files", [])
            for fid in (job.get("input_file_id"), remote.get("output_file_id"), remote.get("error_file_id")):
                if fid and fid not in deleted:
                    try:
                        http.call("/files/" + remote_id(fid), method="DELETE")
                        deleted.append(fid)
                    except PhotographyError as exc:
                        job["cleanup_error"] = exc.to_dict()
                        break
        with store.transaction():
            save_job(store, plan, job)
    result = summarize(store, plan_id)
    # Pending jobs can be explicitly resumed after all submitted jobs are collected.
    if result["status"] == "submitted" and all(j["status"] in ("collected", "rejected") for j in result.get("jobs", [])):
        with store.transaction():
            current = store.plan(plan_id)
            current["status"] = "paused"
            store.save_plan(current)
        result = summarize(store, plan_id)
    return result


def collect_job(plan, job, remote, store, config, http):
    mapping = {r["id"]: r for r in store.requests(plan["plan_id"]) if r["id"] in job["request_ids"]}
    rows, duplicates = {}, set()
    for key in ("output_file_id", "error_file_id"):
        if not remote.get(key):
            continue
        data = http.call("/files/" + remote_id(remote[key]) + "/content", raw=True, max_bytes=200_000_000)
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                raise PhotographyError("INVALID_BATCH_OUTPUT", "A batch result line is invalid; nothing from this collection was committed.") from None
            cid = row.get("custom_id") if isinstance(row, dict) else None
            if not isinstance(cid, str) or cid not in mapping:
                raise PhotographyError("BATCH_MAPPING_ERROR", "Batch output contains an unknown request identifier.")
            if cid in rows:
                duplicates.add(cid)
            rows[cid] = row
    for cid, request in mapping.items():
        if request["status"] in ("completed", "failed", "stale", "cancelled"):
            continue
        row = rows.get(cid)
        usage = None
        try:
            if cid in duplicates:
                raise PhotographyError("DUPLICATE_RESULT", "Duplicate request result; no result selected by position.")
            response = row.get("response") if row else None
            if not isinstance(response, dict) or response.get("status_code") != 200:
                raise PhotographyError("BATCH_ITEM_FAILED", "Request failed, expired, was cancelled, or has no output. Inspect request status before planning a retry.")
            outputs, errors, usage = parse_results(response.get("body"), [s["photo_id"] for s in request["snapshots"]])
            with store.transaction():
                request = next(r for r in store.requests(plan["plan_id"]) if r["id"] == cid)
                if request["status"] in ("completed", "failed", "stale", "cancelled"):
                    continue
                save_outputs(plan, request, store, config, outputs, errors, usage)
        except PhotographyError as exc:
            with store.transaction():
                request = next(r for r in store.requests(plan["plan_id"]) if r["id"] == cid)
                if request["status"] in ("completed", "failed", "stale", "cancelled"):
                    continue
                request["attempts"][-1].update(status="failed", usage=getattr(exc, "usage", usage), completed_at=now())
                fail_request(plan, request, store, exc.to_dict())
    job["status"] = "collected"
    job["collected_at"] = now()
