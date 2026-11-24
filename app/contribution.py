"""各批观测贡献汇总。

口径（按消息时刻的 UTC 自然日）：

- 有效消息数：该批在当前生效数据中仍提供的消息条数；
- 独家消息数：仅由该批提供的消息条数；
- 折算数：每条消息在**仍提供它的所有批次**之间平分 1 条，
  即对某批收取 ``1 / 提供该消息的批次数``，用精确分数累计；
- 去重总数：当天仍然有效的不同消息号条数。

因此同一天各批折算数之和恰好等于去重总数（每条第 1 条被完全拆分）。

汇总按结果版本存储：批次、停靠结果与汇总在同一个 SQLite 写事务内生效，
查询只读该版本的汇总行，不重新扫描消息表，从而保证同一份已生效数据快照。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from typing import Iterable, Optional


def fraction_text(value: Fraction) -> str:
    """精确分数的规范文本表示，例如 ``1/2``、``3/1``。"""
    return f"{value.numerator}/{value.denominator}"


def _day_bounds(day: str) -> tuple[str, str]:
    """UTC 日 [起, 止) 的 ISO-Z 边界，可直接做字符串范围比较。"""
    start = date.fromisoformat(day)
    end = start + timedelta(days=1)
    return f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T00:00:00Z"


def recompute_days(
    conn: sqlite3.Connection, version_id: int, days: Iterable[str]
) -> None:
    """从当前生效消息与来源重建指定 UTC 日的汇总。

    调用方须在批次/消息/来源已更新之后、同一写事务内调用。
    只重建给定日期；未涉及的日期沿用旧版本汇总。
    """
    for day in sorted(set(days)):
        conn.execute(
            "DELETE FROM contribution_batch WHERE version_id=? AND day=?",
            (version_id, day),
        )
        conn.execute(
            "DELETE FROM contribution_day WHERE version_id=? AND day=?",
            (version_id, day),
        )

        start, end = _day_bounds(day)
        rows = conn.execute(
            "SELECT m.msg_no, s.batch_no FROM message m "
            "JOIN message_source s ON s.msg_no = m.msg_no "
            "WHERE m.active=1 AND m.ts >= ? AND m.ts < ?",
            (start, end),
        ).fetchall()
        sources: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            sources[row["msg_no"]].add(row["batch_no"])

        # 即使当天已无任何有效消息，也保留一条 0 总数记录，
        # 便于按日查询看到去重总数归零。
        conn.execute(
            "INSERT INTO contribution_day(version_id, day, dedup_total) "
            "VALUES(?,?,?)",
            (version_id, day, len(sources)),
        )
        if not sources:
            continue

        valid: dict[str, int] = defaultdict(int)
        exclusive: dict[str, int] = defaultdict(int)
        share: dict[str, Fraction] = defaultdict(Fraction)
        for batches in sources.values():
            weight = Fraction(1, len(batches))
            for batch_no in batches:
                valid[batch_no] += 1
                share[batch_no] += weight
                if len(batches) == 1:
                    exclusive[batch_no] += 1

        for batch_no in sorted(valid):
            value = share[batch_no]
            conn.execute(
                "INSERT INTO contribution_batch(version_id, day, batch_no, "
                "valid_count, exclusive_count, share_num, share_den) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    version_id,
                    day,
                    batch_no,
                    valid[batch_no],
                    exclusive[batch_no],
                    value.numerator,
                    value.denominator,
                ),
            )


def copy_version(
    conn: sqlite3.Connection, old_version_id: int, new_version_id: int
) -> None:
    """把上一生效版本的汇总整体复制到新版本，供增量更新。"""
    conn.execute(
        "INSERT INTO contribution_day(version_id, day, dedup_total) "
        "SELECT ?, day, dedup_total FROM contribution_day WHERE version_id=?",
        (new_version_id, old_version_id),
    )
    conn.execute(
        "INSERT INTO contribution_batch(version_id, day, batch_no, valid_count, "
        "exclusive_count, share_num, share_den) "
        "SELECT ?, day, batch_no, valid_count, exclusive_count, share_num, "
        "share_den FROM contribution_batch WHERE version_id=?",
        (new_version_id, old_version_id),
    )


def backfill_if_needed(conn: sqlite3.Connection) -> None:
    """旧库首次启用：若现行版本还没有汇总，按现行消息和来源补建。"""
    version = conn.execute(
        "SELECT version_id FROM result_version WHERE active=1 "
        "ORDER BY version_id DESC"
    ).fetchone()
    if version is None:
        return
    version_id = version["version_id"]
    if conn.execute(
        "SELECT 1 FROM contribution_day WHERE version_id=? LIMIT 1",
        (version_id,),
    ).fetchone():
        return
    days = [
        row["day"]
        for row in conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) AS day FROM message WHERE active=1"
        )
    ]
    if days:
        recompute_days(conn, version_id, days)


# ---------------------------------------------------------------- 查询

_SELECT_DAY_BATCH = (
    "SELECT d.day AS day, d.dedup_total AS dedup_total, "
    "b.batch_no AS batch_no, b.valid_count AS valid_count, "
    "b.exclusive_count AS exclusive_count, b.share_num AS share_num, "
    "b.share_den AS share_den "
    "FROM contribution_day d "
    "LEFT JOIN contribution_batch b "
    "  ON b.version_id = d.version_id AND b.day = d.day "
    "WHERE d.version_id = ?"
)


def _batch_from_row(row: sqlite3.Row) -> Optional[dict]:
    if row["batch_no"] is None:
        return None
    value = Fraction(row["share_num"], row["share_den"])
    text = fraction_text(value)
    return {
        "batch_no": row["batch_no"],
        # 有效消息数
        "valid_message_count": row["valid_count"],
        "valid_count": row["valid_count"],
        "message_count": row["valid_count"],
        # 独家消息数
        "exclusive_message_count": row["exclusive_count"],
        "exclusive_count": row["exclusive_count"],
        # 折算数：精确分数，字符串形式 + 约分后分子分母
        "apportioned_count": text,
        "apportioned": text,
        "share": text,
        "fraction": text,
        "apportioned_numerator": value.numerator,
        "apportioned_denominator": value.denominator,
    }


def _day_from_rows(version_id: int, day: str, rows: list[sqlite3.Row]) -> dict:
    total = rows[0]["dedup_total"]
    batches = [b for b in (_batch_from_row(r) for r in rows) if b is not None]
    return {
        "version_id": version_id,
        "date": day,
        "day": day,
        "utc_date": day,
        # 去重总数
        "dedup_total": total,
        "deduplicated_total": total,
        "unique_total": total,
        "total": total,
        "batches": batches,
        "by_batch": {b["batch_no"]: b for b in batches},
    }


def query_day(
    conn: sqlite3.Connection, version_id: int, day: str
) -> Optional[dict]:
    # 单条 JOIN 语句读取：去重总数与各批份额来自同一快照。
    rows = conn.execute(
        _SELECT_DAY_BATCH + " AND d.day = ? ORDER BY b.batch_no",
        (version_id, day),
    ).fetchall()
    if not rows:
        return None
    return _day_from_rows(version_id, day, rows)


def query_days(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        _SELECT_DAY_BATCH + " ORDER BY d.day, b.batch_no",
        (version_id,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["day"], []).append(row)
    return [
        _day_from_rows(version_id, day, grouped[day]) for day in sorted(grouped)
    ]
