"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    office TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL,
                    record_version INTEGER NOT NULL,
                    evidence_due_day INTEGER,
                    initiated_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_pending
                    ON transfers(record_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_transfers_record ON transfers(record_id, id);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
            if "office" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN office TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _transfer_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item.get("result") else None
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, office: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,office,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, office or "", json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            pending = connection.execute(
                "SELECT id FROM transfers WHERE record_id=? AND status='pending'",
                (record_id,),
            ).fetchall()
            for row in pending:
                connection.execute(
                    "UPDATE transfers SET status='voided', updated_at=? WHERE id=? AND status='pending'",
                    (now, int(row["id"])),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "transfer_voided", actor_id, version, json.dumps({"transfer_id": int(row["id"]), "reason": "case_updated", "by_action": action}, ensure_ascii=False, sort_keys=True), now),
                )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def office_stats(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT office, COUNT(*) AS total FROM records GROUP BY office ORDER BY office"
            ).fetchall()
        return [{"office": str(row["office"]), "total": int(row["total"])} for row in rows]

    def initiate_transfer(
        self,
        record_id: int,
        expected_version: int,
        from_org: str,
        to_org: str,
        evidence_due_day: Optional[int],
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            pending = connection.execute(
                "SELECT id FROM transfers WHERE record_id=? AND status='pending'",
                (record_id,),
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("已有待确认的转办单，请等待接收方处理或作废后重发")
            backfilled = False
            office = row["office"]
            if not office:
                office = from_org
                backfilled = True
                connection.execute(
                    "UPDATE records SET office=?,updated_at=? WHERE id=?",
                    (office, now, record_id),
                )
            try:
                cursor = connection.execute(
                    "INSERT INTO transfers(record_id,status,from_org,to_org,record_version,evidence_due_day,initiated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (record_id, "pending", office, to_org, int(expected_version), evidence_due_day, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("已有转办单先到，本次发起被拒绝") from exc
            transfer_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "transfer_initiated", actor_id, int(row["version"]), json.dumps({"transfer_id": transfer_id, "from_org": office, "to_org": to_org, "evidence_due_day": evidence_due_day, "backfilled_office": backfilled}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return self._transfer_row(result)

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFound("转办单不存在")
        return self._transfer_row(row)

    def list_transfers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transfers WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        return [self._transfer_row(row) for row in rows]

    def pending_transfer(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transfers WHERE record_id=? AND status='pending' ORDER BY id",
                (record_id,),
            ).fetchone()
        return self._transfer_row(row) if row else None

    def confirm_transfer(self, transfer_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("转办单不存在")
            if row["status"] == "confirmed":
                connection.rollback()
                return self._transfer_row(row)
            if row["status"] == "voided":
                connection.rollback()
                raise Conflict("该转办单已作废，案件等待期间发生更新，请重新发起转办")
            record_id = int(row["record_id"])
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record["version"]) != int(row["record_version"]):
                connection.execute(
                    "UPDATE transfers SET status='voided', updated_at=? WHERE id=? AND status='pending'",
                    (now, transfer_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "transfer_voided", actor_id, int(record["version"]), json.dumps({"transfer_id": transfer_id, "reason": "case_updated"}, ensure_ascii=False, sort_keys=True), now),
                )
                connection.commit()
                raise Conflict("案件等待期间已更新，原转办单作废，请重新发起转办")
            from_org = row["from_org"]
            to_org = row["to_org"]
            new_version = int(record["version"]) + 1
            connection.execute(
                "UPDATE records SET office=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (to_org, new_version, actor_id, now, record_id),
            )
            result_snapshot = {
                "record_id": record_id,
                "office": to_org,
                "previous_office": from_org,
                "version": new_version,
            }
            connection.execute(
                "UPDATE transfers SET status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=?,result=? WHERE id=?",
                (actor_id, now, now, json.dumps(result_snapshot, ensure_ascii=False, sort_keys=True), transfer_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "transfer_confirmed", actor_id, new_version, json.dumps({"transfer_id": transfer_id, "from_org": from_org, "to_org": to_org, "evidence_due_day": row["evidence_due_day"], "previous_office": from_org}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return self._transfer_row(result)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
