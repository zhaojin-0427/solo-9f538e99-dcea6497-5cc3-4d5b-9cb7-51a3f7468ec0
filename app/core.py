"""核心领域逻辑：CSV 解析校验与 UTC 小时降雨量计算。

本模块为纯逻辑，不依赖 Flask / SQLite，便于独立测试。

业务规则：
- 每小时按 60 个相邻分钟间隔计算（整点 H:00 到下一整点，共 61 个端点、60 段）。
- 每个间隔：主计两端读数齐全就取主计差值，否则备计两端齐全取备计差值，
  否则该间隔无可用数据；绝不跨设备相减。
- 计数下降按一次 10000 回绕处理：差值 = (末读数 - 初读数) mod 10000。
- 每脉冲折合 0.2 毫米；任一间隔无可用数据则整小时不可供数。
- 同一（站点, 设备, 分钟）键只接受更高修订号的记录。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

PULSE_MM = 0.2            # 每脉冲折合雨量（毫米）
COUNTER_MOD = 10000       # 翻斗计数器量程（0~9999，回绕一次加 10000）
INTERVALS_PER_HOUR = 60   # 每小时 60 个相邻分钟间隔
MINUTE_SECONDS = 60
HOUR_SECONDS = 3600

DEVICE_PRIMARY = "primary"
DEVICE_BACKUP = "backup"
SOURCE_MISSING = "missing"

_DEVICE_ALIASES = {
    "primary": DEVICE_PRIMARY, "pri": DEVICE_PRIMARY, "p": DEVICE_PRIMARY,
    "main": DEVICE_PRIMARY, "master": DEVICE_PRIMARY,
    "主": DEVICE_PRIMARY, "主计": DEVICE_PRIMARY,
    "backup": DEVICE_BACKUP, "bak": DEVICE_BACKUP, "bk": DEVICE_BACKUP,
    "b": DEVICE_BACKUP, "secondary": DEVICE_BACKUP,
    "备": DEVICE_BACKUP, "备计": DEVICE_BACKUP,
}

# 可选表头行的列名别名（小写后匹配）
_HEADER_NAMES = {
    "station": "station", "站点": "station", "站号": "station", "站名": "station",
    "device": "device", "主备": "device", "主备标记": "device", "设备": "device",
    "minute": "minute", "time": "minute", "timestamp": "minute", "datetime": "minute",
    "时间": "minute", "utc时间": "minute", "utc": "minute", "分钟": "minute",
    "counter": "counter", "count": "counter", "计数": "counter", "脉冲计数": "counter", "脉冲": "counter",
    "revision": "revision", "rev": "revision", "修订号": "revision", "修订": "revision",
}
_DEFAULT_ORDER = ["station", "device", "minute", "counter", "revision"]

# YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]，也允许空格分隔日期时间
_MINUTE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*"
    r"(Z|z|[+-]\d{2}:?\d{2})?\s*$"
)


class CsvFormatError(ValueError):
    """文件级 CSV 格式错误（整个文件不可用，不入库任何行）。"""


@dataclass(frozen=True)
class Reading:
    """一条合法的累计脉冲读数。"""

    station: str
    device: str           # DEVICE_PRIMARY / DEVICE_BACKUP
    minute_epoch: int     # UTC 整分钟的 epoch 秒
    counter: int          # 0~9999
    revision: int         # >= 0


@dataclass(frozen=True)
class RowError:
    """单行拒绝记录。"""

    row_no: int           # 在原文件中的行号（从 1 开始）
    reason: str
    raw: str


def iso_utc(epoch: int) -> str:
    """epoch 秒 -> 'YYYY-MM-DDTHH:MM:SSZ'。"""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_minute(text: str) -> int:
    """解析 UTC 整分钟时间，返回 epoch 秒。

    接受 'YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]'（日期时间间也允许空格）。
    未带时区按 UTC 处理；带偏移则换算为 UTC。秒必须为零（允许省略）。
    """
    s = (text or "").strip()
    if "." in s:
        raise ValueError(f"时间不允许秒以下精度: {s!r}")
    m = _MINUTE_RE.match(s)
    if not m:
        raise ValueError(
            f"时间格式无法解析（应为 YYYY-MM-DDTHH:MM，可带秒与 UTC 偏移）: {s!r}"
        )
    date, hh, mm, ss, off = m.groups()
    if ss is not None and ss != "00":
        raise ValueError(f"时间必须为整分钟（秒需为 00）: {s!r}")
    iso = f"{date}T{hh}:{mm}:00"
    if off:
        off = off.upper()
        if off == "Z":
            iso += "+00:00"
        else:
            o = off.replace(":", "")
            iso += f"{o[:3]}:{o[3:]}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"时间无效: {s!r}（{exc}）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 未带时区按 UTC
    return int(dt.astimezone(timezone.utc).timestamp())


def parse_hour(text: str) -> int:
    """解析整点小时参数，返回该小时起始的 epoch 秒；必须整点对齐。"""
    epoch = parse_minute(text)
    if epoch % HOUR_SECONDS != 0:
        raise ValueError(f"小时必须整点对齐（分钟需为 00）: {text!r}")
    return epoch


def parse_counter(text: str) -> int:
    s = (text or "").strip()
    try:
        value = int(s, 10)
    except ValueError:
        raise ValueError(f"计数不是整数: {s!r}") from None
    if not 0 <= value <= 9999:
        raise ValueError(f"计数超出 0~9999 范围: {value}")
    return value


def parse_revision(text: str) -> int:
    s = (text or "").strip()
    try:
        value = int(s, 10)
    except ValueError:
        raise ValueError(f"修订号不是整数: {s!r}") from None
    if value < 0:
        raise ValueError(f"修订号不能为负: {value}")
    return value


def _parse_fields(fields, order) -> Reading:
    vals = {name: fields[i] for i, name in enumerate(order) if name is not None}
    station = vals["station"].strip()
    if not station:
        raise ValueError("站点为空")
    if len(station) > 64:
        raise ValueError("站点名超过 64 字符")
    device = _DEVICE_ALIASES.get(vals["device"].strip().lower())
    if device is None:
        raise ValueError(
            f"无法识别的主备标记: {vals['device'].strip()!r}（应为 primary/backup 或 主/备）"
        )
    return Reading(
        station=station,
        device=device,
        minute_epoch=parse_minute(vals["minute"]),
        counter=parse_counter(vals["counter"]),
        revision=parse_revision(vals["revision"]),
    )


def parse_csv_text(text: str):
    """解析 CSV 文本。

    返回 (valid_rows, row_errors)：
    - valid_rows: [(row_no, raw_line, Reading)]，按文件顺序；
    - row_errors: [RowError]，单行校验失败记录。
    文件级问题（空文件、表头缺列等）抛 CsvFormatError，调用方应整体拒收。
    """
    if text is None or not text.strip():
        raise CsvFormatError("没有收到 CSV 内容")
    try:
        raw_rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise CsvFormatError(f"CSV 解析失败: {exc}") from exc

    # 跳过完全空白的行，行号保留原始文件行号
    rows = [(i + 1, r) for i, r in enumerate(raw_rows) if any(c.strip() for c in r)]
    if not rows:
        raise CsvFormatError("CSV 为空或没有数据行")

    _, first = rows[0]
    mapped = [_HEADER_NAMES.get(c.strip().lower()) for c in first]
    if any(name is not None for name in mapped):
        # 第一行是表头：必须能找齐 5 个必需列，未识别的列忽略
        missing = [n for n in _DEFAULT_ORDER if n not in mapped]
        if missing:
            raise CsvFormatError(f"表头缺少必需列: {', '.join(missing)}")
        order = mapped
        body = rows[1:]
        if not body:
            raise CsvFormatError("CSV 只有表头，没有数据行")
    else:
        order = _DEFAULT_ORDER
        body = rows

    max_idx = max(i for i, name in enumerate(order) if name is not None)
    valid, errors = [], []
    for row_no, fields in body:
        raw = ",".join(fields)
        if len(fields) <= max_idx:
            errors.append(
                RowError(row_no, f"列数不足（需要 {max_idx + 1} 列，实际 {len(fields)} 列）", raw)
            )
            continue
        if any(c.strip() for c in fields[max_idx + 1:]):
            errors.append(RowError(row_no, "存在多余的数据列", raw))
            continue
        try:
            reading = _parse_fields(fields, order)
        except ValueError as exc:
            errors.append(RowError(row_no, str(exc), raw))
            continue
        valid.append((row_no, raw, reading))
    return valid, errors


def counter_delta(start: int, end: int) -> int:
    """相邻两次累计计数之差；计数下降按一次 10000 回绕处理。"""
    return (end - start) % COUNTER_MOD


def compute_hour(counters: dict, station: str, hour_start_epoch: int) -> dict:
    """计算某站某 UTC 整点小时的雨量。

    counters: {(device, minute_epoch): counter}，需覆盖
    [hour_start, hour_start+3600] 的端点（缺失即查不到）。
    返回结果同时供网页展示与 API 输出，保证两者一致。
    """
    if hour_start_epoch % HOUR_SECONDS != 0:
        raise ValueError("hour_start_epoch 必须整点对齐")

    intervals = []
    total_pulses = 0
    missing = 0
    source_counts = {DEVICE_PRIMARY: 0, DEVICE_BACKUP: 0, SOURCE_MISSING: 0}
    for i in range(INTERVALS_PER_HOUR):
        m0 = hour_start_epoch + i * MINUTE_SECONDS
        m1 = m0 + MINUTE_SECONDS
        source, delta = SOURCE_MISSING, None
        # 主计两端齐全 -> 主计差值；否则备计两端齐全 -> 备计差值；不跨设备相减
        p0 = counters.get((DEVICE_PRIMARY, m0))
        p1 = counters.get((DEVICE_PRIMARY, m1))
        if p0 is not None and p1 is not None:
            source, delta = DEVICE_PRIMARY, counter_delta(p0, p1)
        else:
            b0 = counters.get((DEVICE_BACKUP, m0))
            b1 = counters.get((DEVICE_BACKUP, m1))
            if b0 is not None and b1 is not None:
                source, delta = DEVICE_BACKUP, counter_delta(b0, b1)
        source_counts[source] += 1
        if delta is None:
            missing += 1
        else:
            total_pulses += delta
        intervals.append({
            "index": i,
            "start": iso_utc(m0),
            "end": iso_utc(m1),
            "source": source,
            "pulses": delta,
            "rainfall_mm": round(delta * PULSE_MM, 1) if delta is not None else None,
        })

    complete = missing == 0
    return {
        "station": station,
        "hour_start": iso_utc(hour_start_epoch),
        "status": "ok" if complete else "incomplete",
        # 供数字段：任一间隔缺失则为 null，预警模型不得取用
        "rainfall_mm": round(total_pulses * PULSE_MM, 1) if complete else None,
        "total_pulses": total_pulses if complete else None,
        "available_pulses": total_pulses,
        "missing_intervals": missing,
        "source_counts": source_counts,
        "intervals": intervals,
    }
