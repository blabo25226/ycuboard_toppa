"""資料のスナップショットを SQLite に保存し、前回との差分(新規/更新/削除)とダウンロード要否を判定する。"""
import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from src.config import DB_PATH

NEW = "NEW"
UPDATED = "UPDATED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    course_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,          -- サイト側のファイルID（再アップロードで変わる）
    course_name TEXT NOT NULL,
    material_id TEXT,                   -- 資料(まとまり)のID
    material_title TEXT,                -- 資料タイトル
    file_name TEXT NOT NULL,
    updated_on TEXT,                    -- サイト上の登録日
    open_end TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    removed INTEGER DEFAULT 0,
    local_path TEXT,
    file_hash TEXT,
    downloaded_updated_on TEXT,         -- ダウンロード時点の登録日
    downloaded_at TEXT,
    PRIMARY KEY (course_id, resource_id)
)
"""


@dataclass
class Changes:
    """1講義ぶんの差分。new/updated の要素は資料 dict に "reason" を足したもの。"""

    baseline: bool = False  # この講義を初めて見た（全件が初回登録）
    new: List[dict] = field(default_factory=list)
    updated: List[dict] = field(default_factory=list)
    removed: List[dict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new or self.updated or self.removed)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(SCHEMA)


def compute_file_hash(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fingerprint(d) -> tuple:
    return (d["file_name"], d["updated_on"] or "", d["material_title"] or "")


def apply_scan(course_id: str, items: List[dict]) -> Changes:
    """今回取得した資料一覧を前回と比較し、差分を返したうえでスナップショットを更新する。"""
    init_db()
    changes = Changes()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM materials WHERE course_id = ? AND removed = 0", (course_id,)
        ).fetchall()
        known_any = conn.execute(
            "SELECT 1 FROM materials WHERE course_id = ? LIMIT 1", (course_id,)
        ).fetchone()
        changes.baseline = known_any is None

        stored: Dict[str, sqlite3.Row] = {r["resource_id"]: r for r in rows}
        current_ids = {it["resource_id"] for it in items}
        vanished = [r for rid, r in stored.items() if rid not in current_ids]

        for it in items:
            prev = stored.get(it["resource_id"])
            if prev is None:
                # 同じ資料・同じファイル名で ID だけ変わっていれば「差し替え」= 更新
                replaced = next(
                    (r for r in vanished
                     if r["material_id"] == it["material_id"] and r["file_name"] == it["file_name"]),
                    None,
                )
                if replaced is not None:
                    vanished.remove(replaced)
                    changes.updated.append({**it, "reason": "差し替え"})
                elif not changes.baseline:
                    changes.new.append({**it, "reason": NEW})
            elif _fingerprint(prev) != _fingerprint(it):
                changes.updated.append({**it, "reason": "更新"})

            conn.execute(
                """
                INSERT INTO materials (course_id, resource_id, course_name, material_id, material_title,
                                       file_name, updated_on, open_end)
                VALUES (:course_id, :resource_id, :course_name, :material_id, :material_title,
                        :file_name, :updated_on, :open_end)
                ON CONFLICT(course_id, resource_id) DO UPDATE SET
                    course_name = excluded.course_name,
                    material_id = excluded.material_id,
                    material_title = excluded.material_title,
                    file_name = excluded.file_name,
                    updated_on = excluded.updated_on,
                    open_end = excluded.open_end,
                    last_seen = CURRENT_TIMESTAMP,
                    removed = 0
                """,
                it,
            )

        for r in vanished:
            changes.removed.append(dict(r))
            conn.execute(
                "UPDATE materials SET removed = 1 WHERE course_id = ? AND resource_id = ?",
                (course_id, r["resource_id"]),
            )
    return changes


def pending_downloads(course_id: str) -> List[dict]:
    """未ダウンロード、ローカルファイル消失、またはダウンロード後にサイト側が更新された資料。"""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM materials WHERE course_id = ? AND removed = 0 ORDER BY material_id, resource_id",
            (course_id,),
        ).fetchall()

    pending = []
    for r in rows:
        if not r["local_path"] or not Path(r["local_path"]).exists():
            reason = NEW if not r["downloaded_at"] else "ローカルファイル消失"
        elif (r["downloaded_updated_on"] or "") != (r["updated_on"] or ""):
            reason = UPDATED
        else:
            continue
        pending.append({**dict(r), "reason": reason})
    return pending


def record_download(course_id: str, resource_id: str, local_path: str, file_hash: str, updated_on: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE materials SET local_path = ?, file_hash = ?, downloaded_updated_on = ?,
                                 downloaded_at = CURRENT_TIMESTAMP
            WHERE course_id = ? AND resource_id = ?
            """,
            (local_path, file_hash, updated_on, course_id, resource_id),
        )
