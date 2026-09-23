"""设备指标线性校准配置：按有效时间段管理，校准值 = 原始值 * scale + offset。

- 每条配置对 (设备, 指标) 声明一个半开有效区间 [valid_from, valid_to)，
  valid_to 为 null 表示无限期；
- 同一 (设备, 指标) 的有效区间不得重叠：创建时在同一事务内校验并写入
  （调用方持有 ingest_lock 串行化写入），保证任意时刻至多一条配置生效；
- 校准只在 resolve 查询时计算，已存储的原始测量不被改写。
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

# valid_to 为 null（无限期）时参与区间比较的远端哨兵时间；
# 与存储格式同为固定微秒宽度的 UTC ISO 字符串，字典序即时间序
_FAR_FUTURE = "9999-12-31T23:59:59.999999Z"


class CalibrationError(ValueError):
    """校准配置参数非法。"""


class CalibrationConflict(Exception):
    """同一 (设备, 指标) 的有效时间段与已有配置重叠。"""


def normalize_instant(dt: datetime) -> str:
    """归一化为 UTC、固定微秒宽度、Z 结尾的 ISO 字符串；naive 一律按 UTC。

    固定宽度保证字符串字典序与时间序一致，区间比较可直接在 SQL 中完成。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="microseconds")
        + "Z"
    )


def create_calibration(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    valid_from: datetime,
    valid_to: Optional[datetime],
    offset: float,
    scale: float,
) -> int:
    """创建校准配置并返回新配置 id；区间重叠抛 CalibrationConflict。

    重叠校验与插入在同一事务中完成（调用方持有 ingest_lock）。
    """
    start = normalize_instant(valid_from)
    end = normalize_instant(valid_to) if valid_to is not None else None
    if end is not None and start >= end:
        raise CalibrationError("valid_from 必须早于 valid_to")
    # 半开区间重叠判定：existing.valid_from < new.end 且 existing.valid_to > new.start
    overlap = conn.execute(
        """
        SELECT id, valid_from, valid_to FROM calibrations
         WHERE device_id = ? AND metric = ?
           AND valid_from < ?
           AND COALESCE(valid_to, ?) > ?
         ORDER BY id LIMIT 1
        """,
        (device_id, metric, end or _FAR_FUTURE, _FAR_FUTURE, start),
    ).fetchone()
    if overlap is not None:
        raise CalibrationConflict(
            f"设备 {device_id} 指标 {metric} 的有效时间段与已有配置重叠: "
            f"id={overlap['id']} [{overlap['valid_from']}, "
            f"{overlap['valid_to'] or 'null'})"
        )
    cur = conn.execute(
        """
        INSERT INTO calibrations
            (device_id, metric, valid_from, valid_to, offset, scale)
        VALUES (?,?,?,?,?,?)
        """,
        (device_id, metric, start, end, offset, scale),
    )
    return cur.lastrowid


def resolve_calibration(
    conn: sqlite3.Connection, device_id: str, metric: str, at: datetime
) -> Optional[sqlite3.Row]:
    """返回 at 时刻生效的校准配置行；无生效配置返回 None。

    区间互不重叠，故至多命中一条；边界为 [valid_from, valid_to)。
    """
    instant = normalize_instant(at)
    return conn.execute(
        """
        SELECT * FROM calibrations
         WHERE device_id = ? AND metric = ?
           AND valid_from <= ?
           AND COALESCE(valid_to, ?) > ?
         ORDER BY id LIMIT 1
        """,
        (device_id, metric, instant, _FAR_FUTURE, instant),
    ).fetchone()


def apply_calibration(value: float, scale: float, offset: float) -> float:
    """线性校准：校准值 = 原始值 * scale + offset。"""
    return value * scale + offset
