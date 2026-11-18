"""SQLite 持久化层：模式、事务性批量入库、查询。

设计要点：
- readings 以 (station, device, minute_epoch) 为主键，同键只接受更高修订号；
- 每批上传在单个事务中写入（读数 + 批次记录 + 行级明细），
  任何异常整体回滚，不会留下半批新结果；
- 流域定义按版本保存，保存与查询都在快照内完成，配置与读数不会读到混合状态；
- 数据库文件落在磁盘（Docker 中挂载卷），重启后仍可查询。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (station, device, minute_epoch)
);
CREATE INDEX IF NOT EXISTS idx_readings_station_minute
    ON readings (station, minute_epoch);

CREATE TABLE IF NOT EXISTS batches (
    batch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at   TEXT NOT NULL,
    filename      TEXT,
    total_rows    INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_rows (
    batch_id INTEGER NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    row_no   INTEGER NOT NULL,
    outcome  TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected')),
    detail   TEXT,
    raw      TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_rows_batch ON batch_rows (batch_id);

-- 流域定义：每次保存新增一个版本号，旧版本整体保留。
-- 查询时按“单个读事务”取到某个已提交版本的完整定义，
-- 因此绝不会读到新旧混合的站点或权重。
CREATE TABLE IF NOT EXISTS basins (
    basin_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    version    INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS basin_members (
    basin_id   INTEGER NOT NULL REFERENCES basins (basin_id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    station    TEXT    NOT NULL,
    weight_ppm INTEGER NOT NULL CHECK (weight_ppm > 0 AND weight_ppm <= 1000000),
    PRIMARY KEY (basin_id, version, station),
    UNIQUE (basin_id, version, position)
);
CREATE INDEX IF NOT EXISTS idx_basin_members_version
    ON basin_members (basin_id, version, position);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。autocommit 模式，写操作与快照查询显式开事务。"""
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def apply_batch(conn: sqlite3.Connection, valid_rows, row_errors, filename=None) -> dict:
    """在同一事务中应用一批读数；任何异常整体回滚，不留半批结果。

    valid_rows: [(row_no, raw, Reading)]，按文件顺序逐行处理；
    row_errors: 解析阶段已拒绝的 RowError 列表。
    同键（站点,设备,分钟）只接受更高修订号，否则记为拒绝。
    返回批次报告 dict。
    """
    now = _utcnow_iso()
    accepted = []
    rejected = [{"row_no": e.row_no, "reason": e.reason, "raw": e.raw} for e in row_errors]
    total = len(valid_rows) + len(row_errors)

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO batches (uploaded_at, filename, total_rows, accepted_rows, rejected_rows)"
            " VALUES (?, ?, ?, 0, 0)",
            (now, filename, total),
        )
        batch_id = cur.lastrowid

        for row_no, raw, r in valid_rows:
            existing = conn.execute(
                "SELECT revision FROM readings"
                " WHERE station=? AND device=? AND minute_epoch=?",
                (r.station, r.device, r.minute_epoch),
            ).fetchone()
            if existing is not None and r.revision <= existing["revision"]:
                rejected.append({
                    "row_no": row_no,
                    "reason": f"修订号 {r.revision} 不高于已存的 {existing['revision']}，同键只接受更高修订",
                    "raw": raw,
                })
                continue
            if existing is None:
                conn.execute(
                    "INSERT INTO readings (station, device, minute_epoch, counter, revision, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (r.station, r.device, r.minute_epoch, r.counter, r.revision, now),
                )
                action = "inserted"
            else:
                conn.execute(
                    "UPDATE readings SET counter=?, revision=?, updated_at=?"
                    " WHERE station=? AND device=? AND minute_epoch=?",
                    (r.counter, r.revision, now, r.station, r.device, r.minute_epoch),
                )
                action = "updated"
            accepted.append({"row_no": row_no, "action": action, "reading": r})
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'accepted', ?, ?)",
                (batch_id, row_no, action, raw),
            )

        for rej in rejected:
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'rejected', ?, ?)",
                (batch_id, rej["row_no"], rej["reason"], rej["raw"]),
            )

        conn.execute(
            "UPDATE batches SET accepted_rows=?, rejected_rows=? WHERE batch_id=?",
            (len(accepted), len(rejected), batch_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    rejected.sort(key=lambda x: x["row_no"])
    return {
        "batch_id": batch_id,
        "filename": filename,
        "uploaded_at": now,
        "total_rows": total,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def fetch_hour_counters(conn: sqlite3.Connection, station: str, hour_start_epoch: int) -> dict:
    """取某站某小时 61 个端点（含下一整点）的全部读数。"""
    rows = conn.execute(
        "SELECT device, minute_epoch, counter FROM readings"
        " WHERE station=? AND minute_epoch BETWEEN ? AND ?",
        (station, hour_start_epoch, hour_start_epoch + 3600),
    ).fetchall()
    return {(r["device"], r["minute_epoch"]): r["counter"] for r in rows}


def _basin_from_row(conn: sqlite3.Connection, row) -> core.BasinConfig | None:
    """按 basins 行读取该版本的成员；版本号已消失（不可能）则返回 None。"""
    if row is None:
        return None
    members = conn.execute(
        "SELECT station, weight_ppm FROM basin_members"
        " WHERE basin_id=? AND version=? ORDER BY position",
        (row["basin_id"], row["version"]),
    ).fetchall()
    return core.BasinConfig(
        name=row["name"],
        members=tuple(
            core.BasinMember(station=m["station"], weight_ppm=m["weight_ppm"])
            for m in members
        ),
    )


def get_basin(conn: sqlite3.Connection, name: str):
    """读取当前已提交的流域定义，返回 (BasinConfig, version, updated_at) 或 (None, ...)。"""
    row = conn.execute(
        "SELECT basin_id, name, version, updated_at FROM basins WHERE name=?", (name,)
    ).fetchone()
    basin = _basin_from_row(conn, row)
    if basin is None:
        return None, None, None
    return basin, row["version"], row["updated_at"]


def get_basin_by_id(conn: sqlite3.Connection, basin_id: int):
    row = conn.execute(
        "SELECT basin_id, name, version, updated_at FROM basins WHERE basin_id=?",
        (basin_id,),
    ).fetchone()
    basin = _basin_from_row(conn, row)
    if basin is None:
        return None, None, None
    return basin, row["version"], row["updated_at"]


def list_basins(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT b.basin_id, b.name, b.version, b.updated_at,"
        " (SELECT COUNT(*) FROM basin_members m"
        "   WHERE m.basin_id=b.basin_id AND m.version=b.version) AS member_count"
        " FROM basins b ORDER BY b.name"
    ).fetchall()


def save_basin(conn: sqlite3.Connection, basin: core.BasinConfig) -> dict:
    """在一个事务中提交流域定义的新版本。

    名称已存在则版本号 +1 并整体替换成员；不存在则新建（版本 1）。
    任何异常整体回滚，旧定义（含旧成员与旧版本号）保持原样。
    """
    now = _utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT basin_id, version FROM basins WHERE name=?", (basin.name,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO basins (name, version, updated_at) VALUES (?, 1, ?)",
                (basin.name, now),
            )
            basin_id, version = cur.lastrowid, 1
        else:
            # 版本号单调递增：先切换指针，再写成员；同一事务内查询看不到中间态
            basin_id, version = row["basin_id"], row["version"] + 1
            conn.execute(
                "UPDATE basins SET version=?, updated_at=? WHERE basin_id=?",
                (version, now, basin_id),
            )
        conn.executemany(
            "INSERT INTO basin_members (basin_id, version, position, station, weight_ppm)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (basin_id, version, i, m.station, m.weight_ppm)
                for i, m in enumerate(basin.members)
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {
        "basin_id": basin_id,
        "name": basin.name,
        "version": version,
        "updated_at": now,
    }


def fetch_basin_hour_snapshot(conn: sqlite3.Connection, name: str,
                              hour_start_epoch: int) -> dict | None:
    """在单个读事务中同时取流域定义与该小时全部成员站读数。

    SQLite 的读事务在 BEGIN 时固定快照（WAL 下同样成立），因此：
    - 配置保存（整份定义换版）与 CSV 补传（整批读数提交）都不会从中间状态读到；
    - 配置、全部站点雨量必然来自同一个已提交状态，不会混用同批补传前后的读数。
    流域不存在时返回 None。
    """
    conn.execute("BEGIN")
    try:
        basin, version, updated_at = get_basin(conn, name)
        if basin is None:
            conn.execute("COMMIT")
            return None
        counters = {
            m.station: fetch_hour_counters(conn, m.station, hour_start_epoch)
            for m in basin.members
        }
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {
        "basin": basin,
        "version": version,
        "updated_at": updated_at,
        "counters": counters,
    }


def list_stations(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT station, COUNT(*) AS n, MIN(minute_epoch) AS first_minute,"
        " MAX(minute_epoch) AS last_minute"
        " FROM readings GROUP BY station ORDER BY station"
    ).fetchall()


def list_batches(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        "SELECT * FROM batches ORDER BY batch_id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: int):
    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        return None, []
    rows = conn.execute(
        "SELECT row_no, outcome, detail, raw FROM batch_rows"
        " WHERE batch_id=? ORDER BY row_no",
        (batch_id,),
    ).fetchall()
    return batch, rows
