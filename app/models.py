from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _aware(dt: datetime) -> datetime:
    """统一为带时区时刻：裸时间按 UTC 解释，便于跨批次正确排序。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, default="")

    vats: Mapped[list["Vat"]] = relationship(back_populates="workshop")


class Vat(Base):
    __tablename__ = "vats"
    __table_args__ = (
        UniqueConstraint("workshop_id", "code", name="uniq_vat_code_per_workshop"),
    )

    STATUS_IDLE = "idle"
    STATUS_REDUCING = "reducing"
    STATUS_READY = "ready"
    STATUSES = frozenset({STATUS_IDLE, STATUS_REDUCING, STATUS_READY})

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(40))
    dyeType: Mapped[str] = mapped_column(String(80))
    volumeL: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_IDLE)

    workshop: Mapped["Workshop"] = relationship(back_populates="vats")
    lots: Mapped[list["DipLot"]] = relationship(back_populates="vat")

    def sorted_lots(self) -> list["DipLot"]:
        """按真实时刻升序（时刻相同再按 id），naive/aware 混存也不会报错。"""
        return sorted(self.lots, key=lambda x: (_aware(x.dippedAt), x.id))

    def latest_lot(self) -> Optional["DipLot"]:
        lots = self.sorted_lots()
        return lots[-1] if lots else None


class DipLot(Base):
    __tablename__ = "dip_lots"
    __table_args__ = (
        # 残行双保险：布米必须为正；电位若有值必须是有限数（NaN != NaN）
        CheckConstraint('"clothMeters" > 0', name="chk_diplot_meters_positive"),
        # PG 里 NaN = NaN 为真，必须直接与 'NaN' 比较才能拦住它；无穷无法存入 numeric
        CheckConstraint(
            "\"redoxMv\" IS NULL OR \"redoxMv\" <> 'NaN'::numeric",
            name="chk_diplot_redox_finite",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vat_id: Mapped[int] = mapped_column(ForeignKey("vats.id", ondelete="CASCADE"))
    dippedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clothMeters: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    redoxMv: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    vat: Mapped["Vat"] = relationship(back_populates="lots")
