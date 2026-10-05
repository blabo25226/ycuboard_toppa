"""資料のスナップショットを SQLite に保存し、前回との差分(新規/更新/削除)とダウンロード要否を判定する。"""
import hashlib
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List

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

# どの講義のどの種類(教材/テスト/課題)を、すでに一度確認したか。
# 「最初は0件だった講義に、後から最初の1件が出た」を新着として扱うために、行の有無ではなくこの記録で判断する。
SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    course_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- material / test / report
    first_scanned TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (course_id, kind)
)
"""

# テスト・課題。ダウンロードや提出はせず、追加・更新・削除の通知だけに使う。
COURSEWORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS coursework (
    course_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- test / report
    item_id TEXT NOT NULL,              -- examinationId / reportId
    course_name TEXT NOT NULL,
    title TEXT,
    period TEXT,                        -- 解答期間・提出期間
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    removed INTEGER DEFAULT 0,
    PRIMARY KEY (course_id, kind, item_id)
)
"""


# Teams（SharePoint）のファイル。キーは SharePoint のファイルID(UniqueId)。
TEAMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams_files (
    course_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    course_name TEXT NOT NULL,
    rel_path TEXT NOT NULL,             -- General からの相対パス（"/" 区切り）
    modified TEXT,                      -- サイト上の更新日時
    size INTEGER,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    removed INTEGER DEFAULT 0,
    local_path TEXT,
    file_hash TEXT,
    downloaded_modified TEXT,           -- ダウンロード時点の更新日時
    downloaded_at TEXT,
    PRIMARY KEY (course_id, file_id)
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


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """コミットして必ず閉じる接続（sqlite3 の with だけでは閉じず、Windows ではファイルがロックされたままになる）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(SCHEMA)
        conn.execute(SCANS_SCHEMA)
        conn.execute(COURSEWORK_SCHEMA)
        conn.execute(TEAMS_SCHEMA)


def _scanned(conn, course_id: str, kind: str) -> bool:
    return conn.execute("SELECT 1 FROM scans WHERE course_id = ? AND kind = ?", (course_id, kind)).fetchone() is not None


def _mark_scanned(conn, course_id: str, kind: str) -> None:
    conn.execute("INSERT OR IGNORE INTO scans (course_id, kind) VALUES (?, ?)", (course_id, kind))


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
        changes.baseline = known_any is None and not _scanned(conn, course_id, "material")
        _mark_scanned(conn, course_id, "material")

        stored: Dict[str, sqlite3.Row] = {r["resource_id"]: r for r in rows}
        current_ids = {it["resource_id"] for it in items}
        vanished = [r for rid, r in stored.items() if rid not in current_ids]

        replacements = []  # (旧行, 新しい resource_id)
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
                    replacements.append((replaced, it["resource_id"]))
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

        for old, new_rid in replacements:
            # 旧版のローカルファイルを新しい ID に引き継ぎ、downloaded_updated_on を空にして「更新」として再取得させる
            # （ダウンロード時に旧版を退避し、同じファイル名で保存し直す）
            conn.execute(
                """
                UPDATE materials SET local_path = ?, file_hash = ?, downloaded_updated_on = NULL, downloaded_at = ?
                WHERE course_id = ? AND resource_id = ? AND local_path IS NULL
                """,
                (old["local_path"], old["file_hash"], old["downloaded_at"], course_id, new_rid),
            )
            conn.execute(
                "UPDATE materials SET removed = 1 WHERE course_id = ? AND resource_id = ?",
                (course_id, old["resource_id"]),
            )

        for r in vanished:
            changes.removed.append(dict(r))
            conn.execute(
                "UPDATE materials SET removed = 1 WHERE course_id = ? AND resource_id = ?",
                (course_id, r["resource_id"]),
            )
    return changes


def has_active_materials(course_id: str) -> bool:
    """前回の記録で、この講義に（削除されていない）資料があったか。"""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM materials WHERE course_id = ? AND removed = 0 LIMIT 1", (course_id,)
        ).fetchone()
    return row is not None


@contextmanager
def scratch_db():
    """一時的に履歴DBの複製を使う（予行演習で本物の記録を書き換えないため）。"""
    global DB_PATH
    original = DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "history.db"
        if original.exists():
            shutil.copy2(original, copy)
        DB_PATH = copy
        try:
            yield
        finally:
            DB_PATH = original


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
        elif r["downloaded_updated_on"] is None or r["downloaded_updated_on"] != (r["updated_on"] or ""):
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


# ---------- テスト・課題 ----------
KIND_LABEL = {"test": "テスト", "report": "課題"}


def apply_coursework(course_id: str, kind: str, items: List[dict]) -> Changes:
    """今回取得したテスト／課題の一覧を前回と比較して差分を返し、記録を更新する（更新判定はタイトルと期間）。"""
    init_db()
    changes = Changes()
    with get_connection() as conn:
        any_row = conn.execute(
            "SELECT 1 FROM coursework WHERE course_id = ? AND kind = ? LIMIT 1", (course_id, kind)
        ).fetchone()
        changes.baseline = any_row is None and not _scanned(conn, course_id, kind)

        stored = {
            r["item_id"]: r
            for r in conn.execute(
                "SELECT * FROM coursework WHERE course_id = ? AND kind = ? AND removed = 0", (course_id, kind)
            )
        }
        current_ids = {it["item_id"] for it in items}

        for it in items:
            prev = stored.get(it["item_id"])
            if prev is None:
                if not changes.baseline:
                    changes.new.append({**it, "reason": "追加"})
            elif (prev["title"], prev["period"] or "") != (it["title"], it["period"] or ""):
                changes.updated.append({**it, "reason": "更新", "previous_period": prev["period"] or ""})
            conn.execute(
                """
                INSERT INTO coursework (course_id, kind, item_id, course_name, title, period)
                VALUES (:course_id, :kind, :item_id, :course_name, :title, :period)
                ON CONFLICT(course_id, kind, item_id) DO UPDATE SET
                    course_name = excluded.course_name, title = excluded.title, period = excluded.period,
                    last_seen = CURRENT_TIMESTAMP, removed = 0
                """,
                {**it, "kind": kind, "course_id": course_id},
            )

        for item_id, r in stored.items():
            if item_id not in current_ids:
                changes.removed.append(dict(r))
                conn.execute(
                    "UPDATE coursework SET removed = 1 WHERE course_id = ? AND kind = ? AND item_id = ?",
                    (course_id, kind, item_id),
                )
        _mark_scanned(conn, course_id, kind)
    return changes


def has_active_coursework(course_id: str, kind: str) -> bool:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM coursework WHERE course_id = ? AND kind = ? AND removed = 0 LIMIT 1", (course_id, kind)
        ).fetchone()
    return row is not None


# ---------- Teams のファイル ----------
def apply_teams_scan(course_id: str, course_name: str, items: List[dict]) -> Changes:
    """今回取得した Teams のファイル一覧を前回と比較して差分を返し、記録を更新する（更新判定は更新日時とサイズ）。"""
    init_db()
    changes = Changes()
    with get_connection() as conn:
        any_row = conn.execute("SELECT 1 FROM teams_files WHERE course_id = ? LIMIT 1", (course_id,)).fetchone()
        changes.baseline = any_row is None and not _scanned(conn, course_id, "teams")
        stored = {
            r["file_id"]: r
            for r in conn.execute("SELECT * FROM teams_files WHERE course_id = ? AND removed = 0", (course_id,))
        }
        current_ids = {it["file_id"] for it in items}

        for it in items:
            prev = stored.get(it["file_id"])
            if prev is None:
                if not changes.baseline:
                    changes.new.append({**it, "reason": NEW})
            elif (prev["modified"], prev["size"]) != (it["modified"], it["size"]):
                changes.updated.append({**it, "reason": "更新"})
            conn.execute(
                """
                INSERT INTO teams_files (course_id, file_id, course_name, rel_path, modified, size)
                VALUES (:course_id, :file_id, :course_name, :rel_path, :modified, :size)
                ON CONFLICT(course_id, file_id) DO UPDATE SET
                    course_name = excluded.course_name, rel_path = excluded.rel_path,
                    modified = excluded.modified, size = excluded.size,
                    last_seen = CURRENT_TIMESTAMP, removed = 0
                """,
                {**it, "course_id": course_id, "course_name": course_name},
            )

        for file_id, r in stored.items():
            if file_id not in current_ids:
                changes.removed.append(dict(r))
                conn.execute(
                    "UPDATE teams_files SET removed = 1 WHERE course_id = ? AND file_id = ?", (course_id, file_id)
                )
        _mark_scanned(conn, course_id, "teams")
    return changes


def has_active_teams_files(course_id: str) -> bool:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM teams_files WHERE course_id = ? AND removed = 0 LIMIT 1", (course_id,)
        ).fetchone()
    return row is not None


def pending_teams_downloads(course_id: str) -> List[dict]:
    """未ダウンロード、ローカルファイル消失、またはダウンロード後にサイト側が更新された Teams のファイル。"""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM teams_files WHERE course_id = ? AND removed = 0 ORDER BY rel_path", (course_id,)
        ).fetchall()
    pending = []
    for r in rows:
        if not r["local_path"] or not Path(r["local_path"]).exists():
            reason = NEW if not r["downloaded_at"] else "ローカルファイル消失"
        elif r["downloaded_modified"] != (r["modified"] or ""):
            reason = UPDATED
        else:
            continue
        pending.append({**dict(r), "reason": reason})
    return pending


def record_teams_download(course_id: str, file_id: str, local_path: str, file_hash: str, modified: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE teams_files SET local_path = ?, file_hash = ?, downloaded_modified = ?,
                                   downloaded_at = CURRENT_TIMESTAMP
            WHERE course_id = ? AND file_id = ?
            """,
            (local_path, file_hash, modified or "", course_id, file_id),
        )
