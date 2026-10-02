"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, PermissionDenied


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 转办等待期间禁止双方执行的动作。
PENDING_BLOCKED_ACTIONS = {"submit", "decide"}
VOID_ON_UPDATE_REASON = "案件在转办等待期间已更新，请由转出方重新发起"
RESEND_MESSAGE = "转办单已作废：等待期间案件已更新，请转出方重新发起"


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
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    from_office TEXT NOT NULL,
                    to_office TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    deadline_snapshot TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    voided_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_pending
                    ON transfers(record_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_transfers_record ON transfers(record_id, id);
                """
            )
            # 旧库升级：补归属办事处列。
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            if "owning_office" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN owning_office TEXT")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _transfer_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["deadline_snapshot"] = json.loads(item["deadline_snapshot"])
        return item

    @staticmethod
    def _insert_audit(connection: sqlite3.Connection, record_id: int, action: str, actor_id: str,
                      version: int, details: Dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str,
               owning_office: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        office = (owning_office or "").strip() or None
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,owning_office)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor_id, actor_id, now, now, office),
                )
                record_id = int(cursor.lastrowid)
                self._insert_audit(connection, record_id, "created", actor_id, 1,
                                   {"state": state, "office": office}, now)
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any],
               actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
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
            # 转办等待期间，双方均不能提交或决定（与作废逻辑同事务，消除竞态）。
            pending = connection.execute(
                "SELECT id FROM transfers WHERE record_id=? AND status='pending'", (record_id,)
            ).fetchone()
            if pending is not None and action in PENDING_BLOCKED_ACTIONS:
                connection.rollback()
                raise Conflict("转办确认前双方均不能提交或决定")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            # 案件在等待期间被更新：旧转办单随本次写入一并作废。
            if pending is not None:
                connection.execute(
                    "UPDATE transfers SET status='voided',voided_reason=?,updated_at=? WHERE id=?",
                    (VOID_ON_UPDATE_REASON, now, int(pending["id"])),
                )
                self._insert_audit(connection, record_id, "transfer_voided", actor_id, version,
                                   {"transfer_id": int(pending["id"]), "reason": VOID_ON_UPDATE_REASON,
                                    "by_action": action}, now)
            self._insert_audit(connection, record_id, action, actor_id, version, details, now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 两阶段转办 ----

    def get_pending_transfer(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transfers WHERE record_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return self._transfer_row(row) if row is not None else None

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
                "SELECT * FROM transfers WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [self._transfer_row(row) for row in rows]

    def create_transfer(self, record_id: int, expected_version: int, from_office: str, to_office: str,
                        deadline_snapshot: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """第一阶段：按当前修订号发起转办；旧数据在同一事务内回填归属办事处。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT version, owning_office FROM records WHERE id=?", (record_id,)
            ).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("案件修订号已变化，请刷新后重新发起转办")
            current_office = record["owning_office"]
            if current_office:
                if current_office != from_office:
                    connection.rollback()
                    raise PermissionDenied("案件当前不属于该办事处，无法发起转办")
            else:
                # 旧数据首次转办前回填当前办事处（不占用修订号）。
                connection.execute(
                    "UPDATE records SET owning_office=?,updated_by=?,updated_at=? WHERE id=?",
                    (from_office, actor_id, now, record_id),
                )
                self._insert_audit(connection, record_id, "office_backfilled", actor_id,
                                   int(record["version"]), {"office": from_office}, now)
            existing = connection.execute(
                "SELECT id FROM transfers WHERE record_id=? AND status='pending'", (record_id,)
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该案件已有进行中的转办单")
            try:
                cursor = connection.execute(
                    "INSERT INTO transfers(record_id,from_office,to_office,expected_version,deadline_snapshot,"
                    "status,created_by,created_at,updated_at) VALUES(?,?,?,?,?, 'pending', ?,?,?)",
                    (record_id, from_office, to_office, int(expected_version),
                     json.dumps(deadline_snapshot, ensure_ascii=False, sort_keys=True), actor_id, now, now),
                )
            except sqlite3.IntegrityError:
                # 两个办事处同时转同一案：唯一部分索引只放行先到的一单。
                connection.rollback()
                raise Conflict("该案件已有进行中的转办单")
            transfer_id = int(cursor.lastrowid)
            self._insert_audit(connection, record_id, "transfer_initiated", actor_id,
                               int(record["version"]),
                               {"transfer_id": transfer_id, "from_office": from_office,
                                "to_office": to_office, "expected_version": int(expected_version),
                                "deadline_snapshot": deadline_snapshot}, now)
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return self._transfer_row(row)

    def confirm_transfer(self, transfer_id: int, actor_id: str) -> Dict[str, Any]:
        """第二阶段：接收方确认，归属一次切换；重复确认返回同一结果。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if transfer is None:
                connection.rollback()
                raise NotFound("转办单不存在")
            if transfer["status"] == "confirmed":
                # 幂等：已确认过，直接返回同一结果，不重复切换。
                connection.commit()
                return self._transfer_row(transfer)
            if transfer["status"] == "voided":
                connection.rollback()
                raise Conflict(transfer["voided_reason"] or RESEND_MESSAGE)
            record = connection.execute(
                "SELECT version, owning_office FROM records WHERE id=?", (transfer["record_id"],)
            ).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record["version"]) != int(transfer["expected_version"]) or (
                record["owning_office"] and record["owning_office"] != transfer["from_office"]
            ):
                # 防御性兜底：等待期间修订号已变（正常流程会在写入时先作废）。
                connection.execute(
                    "UPDATE transfers SET status='voided',voided_reason=?,updated_at=? WHERE id=?",
                    (VOID_ON_UPDATE_REASON, now, transfer_id),
                )
                self._insert_audit(connection, int(transfer["record_id"]), "transfer_voided", actor_id,
                                   int(record["version"]),
                                   {"transfer_id": transfer_id, "reason": VOID_ON_UPDATE_REASON,
                                    "by_action": "revision_check"}, now)
                connection.commit()
                raise Conflict(RESEND_MESSAGE)
            # 归属一次切换：只改归属列，案件内容与修订号不变。
            connection.execute(
                "UPDATE records SET owning_office=?,updated_by=?,updated_at=? WHERE id=?",
                (transfer["to_office"], actor_id, now, transfer["record_id"]),
            )
            connection.execute(
                "UPDATE transfers SET status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
                (actor_id, now, now, transfer_id),
            )
            self._insert_audit(connection, int(transfer["record_id"]), "transfer_confirmed", actor_id,
                               int(record["version"]),
                               {"transfer_id": transfer_id, "from_office": transfer["from_office"],
                                "to_office": transfer["to_office"]}, now)
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return self._transfer_row(row)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self._insert_audit(connection, record_id, action, actor_id, int(row["version"]), details, _now())

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

    def stats_by_office(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT owning_office, COUNT(*) AS total FROM records GROUP BY owning_office"
            ).fetchall()
        result: Dict[str, int] = {}
        for row in rows:
            office = row["owning_office"] or "unassigned"
            result[str(office)] = int(row["total"])
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
