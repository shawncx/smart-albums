"""Persistent plans, request journals and conservative cross-process claims."""
import json
from .config import PhotographyError

WORKFLOW_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS analysis_settings (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS analysis_plans (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS analysis_plan_items (plan_id TEXT NOT NULL REFERENCES analysis_plans(id), photo_id TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(plan_id,photo_id))",
    "CREATE TABLE IF NOT EXISTS analysis_requests (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES analysis_plans(id), data_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS requests_plan ON analysis_requests(plan_id)",
    "CREATE TABLE IF NOT EXISTS analysis_claims (cache_key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES analysis_requests(id))",
)


class WorkflowStorage:
    def settings(self):
        row = self.db.execute("SELECT data_json FROM analysis_settings WHERE id='default'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_settings(self, settings):
        self.db.execute("INSERT OR REPLACE INTO analysis_settings VALUES ('default',?)", (json.dumps(settings),))

    def plan(self, plan_id):
        row = self.db.execute("SELECT data_json FROM analysis_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise PhotographyError("PLAN_NOT_FOUND", "Analysis plan does not exist.")
        return json.loads(row[0])

    def save_plan(self, plan):
        self.db.execute("INSERT INTO analysis_plans VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                        (plan["plan_id"], json.dumps(plan, ensure_ascii=False)))

    def plan_items(self, plan_id):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data_json FROM analysis_plan_items WHERE plan_id=? ORDER BY rowid", (plan_id,))]

    def save_item(self, plan_id, item):
        self.db.execute("INSERT INTO analysis_plan_items VALUES (?,?,?) ON CONFLICT(plan_id,photo_id) DO UPDATE SET data_json=excluded.data_json",
                        (plan_id, item["photo_id"], json.dumps(item, ensure_ascii=False)))

    def requests(self, plan_id):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data_json FROM analysis_requests WHERE plan_id=? ORDER BY rowid", (plan_id,))]

    def save_request(self, request):
        self.db.execute("INSERT INTO analysis_requests VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                        (request["id"], request["plan_id"], json.dumps(request, ensure_ascii=False)))

    def claim(self, request, keys):
        for key in keys:
            row = self.db.execute("SELECT request_id FROM analysis_claims WHERE cache_key=?", (key,)).fetchone()
            if row and row[0] != request["id"]:
                raise PhotographyError("ANALYSIS_IN_PROGRESS", "Another request already owns this input. Inspect its saved plan before retrying.")
        self.save_request(request)
        for key in keys:
            self.db.execute("INSERT OR IGNORE INTO analysis_claims VALUES (?,?)", (key, request["id"]))

    def release(self, request):
        self.db.execute("DELETE FROM analysis_claims WHERE request_id=?", (request["id"],))
