"""审计结论冻结存储。

同一 audit_id：
* 重传完全相同的载荷 -> 回放原结论（不重新分析、不覆盖记录）；
* 改换载荷         -> 拒绝（冲突），已冻结记录保持不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Any


def canonical_hash(payload: Any) -> str:
    """载荷的规范化哈希：与字段顺序无关，只取决于内容。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class FrozenStore:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audits (
                    audit_id      TEXT PRIMARY KEY,
                    payload_hash  TEXT NOT NULL,
                    conclusion    TEXT NOT NULL,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

    def submit(self, audit_id: str, payload: Any, conclusion: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """提交冻结。

        返回 (outcome, stored_conclusion)：
        * ("created", 结论) 首次冻结；
        * ("replayed", 原结论) 同载荷重传；
        * ("conflict", None) 同 audit_id 不同载荷，拒绝。
        """
        digest = canonical_hash(payload)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT payload_hash, conclusion FROM audits WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] == digest:
                    return "replayed", json.loads(row["conclusion"])
                return "conflict", None
            self._conn.execute(
                "INSERT INTO audits (audit_id, payload_hash, conclusion) VALUES (?, ?, ?)",
                (audit_id, digest, json.dumps(conclusion, ensure_ascii=False)),
            )
        return "created", conclusion

    def get(self, audit_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT conclusion FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        return json.loads(row["conclusion"]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
