from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import Vat

# 与数据库列精度保持一致的硬边界
MAX_METERS = Decimal("99999999.99")  # Numeric(10, 2)
MAX_REDOX = Decimal("999999.99")    # Numeric(8, 2)


class WorkshopIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    region: str = Field(min_length=1, max_length=80)
    notes: str = ""


class VatIn(BaseModel):
    workshop_id: int
    code: str = Field(min_length=1, max_length=40)
    dyeType: str = Field(min_length=1, max_length=80)
    volumeL: Decimal
    status: str = Vat.STATUS_IDLE

    @field_validator("status")
    @classmethod
    def _known_status(cls, v: str) -> str:
        if v not in Vat.STATUSES:
            raise ValueError("未知缸状态")
        return v

    @field_validator("volumeL")
    @classmethod
    def _positive_volume(cls, v: Decimal) -> Decimal:
        if not v.is_finite() or v <= 0:
            raise ValueError("缸容须为正数")
        return v


class DipLotIn(BaseModel):
    vat_id: int
    dippedAt: datetime
    clothMeters: Decimal
    redoxMv: Optional[Decimal] = None

    @field_validator("dippedAt")
    @classmethod
    def _aware_time(cls, v: datetime) -> datetime:
        # 裸时间一律按 UTC 解释，杜绝 naive/aware 混排
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("clothMeters")
    @classmethod
    def _positive_meters(cls, v: Decimal) -> Decimal:
        if not v.is_finite() or v <= 0:
            raise ValueError("布米须为正数")
        if v > MAX_METERS:
            raise ValueError("布米超出允许范围")
        return v

    @field_validator("redoxMv")
    @classmethod
    def _finite_redox(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None:
            if not v.is_finite():
                raise ValueError("电位必须是有限数值")
            if abs(v) > MAX_REDOX:
                raise ValueError("电位超出允许范围")
        return v


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    detail: str
