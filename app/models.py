"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class AISMessageIn(BaseModel):
    msg_no: str = Field(min_length=1, description="消息号")
    ship_no: str = Field(min_length=1, description="船号")
    ts: str = Field(description="UTC 时刻，ISO-8601，如 2026-11-24T08:00:00Z")
    lon: float = Field(ge=-180.0, le=180.0, description="经度")
    lat: float = Field(ge=-90.0, le=90.0, description="纬度")

    @field_validator("ts")
    @classmethod
    def _check_utc(cls, v: str) -> str:
        from .engine import ValidationError, parse_utc

        try:
            parse_utc(v)  # 抛异常即 422
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        return v


class BatchSubmit(BaseModel):
    messages: list[AISMessageIn] = Field(min_length=1)


class PointOut(BaseModel):
    msg_no: str
    ts: str
    lon: float
    lat: float
    in_port: bool
    source_batches: list[str]


class StopRecordOut(BaseModel):
    ship_no: str
    seg_no: int
    run_no: int
    seg_key: str
    entry_point: PointOut
    exit_point: PointOut
    before_point: PointOut
    after_point: PointOut
    kind: Literal["stop"] = "stop"


class PendingRecordOut(BaseModel):
    ship_no: str
    seg_no: int
    run_no: int
    seg_key: str
    entry_point: PointOut
    exit_point: PointOut
    before_point: Optional[PointOut]
    after_point: Optional[PointOut]
    reason: str
    kind: Literal["pending"] = "pending"


class BatchResult(BaseModel):
    batch_no: str
    mode: Literal["submit", "replace"]
    status: Literal["accepted", "rejected"]
    version_id: Optional[int] = None
    error: Optional[str] = None
    messages_accepted: int
    affected_ships: list[str] = []
    reused_records: int = 0
    recomputed_records: int = 0


class BatchInfo(BaseModel):
    batch_no: str
    created_at: str
    replaced_at: Optional[str]
    active: bool
    message_count: int


class VersionInfo(BaseModel):
    version_id: int
    created_at: str
    batches: list[str]
    active: bool


class BatchContributionOut(BaseModel):
    batch_no: str
    # 有效消息数（该批仍提供的消息条数）
    valid_message_count: int = Field(description="有效消息数")
    valid_count: int
    message_count: int
    # 独家消息数（仅该批提供的条数）
    exclusive_message_count: int = Field(description="独家消息数")
    exclusive_count: int
    # 折算数：精确分数，各批之和等于当天去重总数
    apportioned_count: str = Field(description="折算数，精确分数文本，如 3/2")
    apportioned: str
    share: str
    fraction: str
    apportioned_numerator: int = Field(description="折算数分子（约分后）")
    apportioned_denominator: int = Field(description="折算数分母（约分后）")


class DayContributionOut(BaseModel):
    version_id: int
    date: str = Field(description="UTC 自然日 YYYY-MM-DD")
    day: str
    utc_date: str
    # 当天去重后的有效消息总数
    dedup_total: int = Field(description="当天去重后的有效消息总数")
    deduplicated_total: int
    unique_total: int
    total: int
    batches: list[BatchContributionOut]
    by_batch: dict[str, BatchContributionOut]


class BatchContributionDayOut(BaseModel):
    batch_no: str
    version_id: int
    date: str
    valid_message_count: int
    exclusive_message_count: int
    apportioned_count: str
    apportioned: str
    share: str
    fraction: str
    apportioned_numerator: int
    apportioned_denominator: int
    valid_count: int
    message_count: int
    exclusive_count: int
