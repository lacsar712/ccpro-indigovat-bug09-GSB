import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.schemas import DipLotIn
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


# ----------------------------- 入参解析 -----------------------------

def _parse_decimal(raw: str, field: str) -> Decimal:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"{field}不能为空")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{field}必须是数字")
    if not value.is_finite():
        raise ValueError(f"{field}必须是有限数值，不能为 NaN 或无穷")
    return value


def _parse_optional_decimal(raw: str, field: str) -> Optional[Decimal]:
    raw = (raw or "").strip()
    if not raw:
        return None
    return _parse_decimal(raw, field)


def _parse_dipped_at(raw: str, offset_minutes: str) -> datetime:
    """解析 datetime-local，并统一成带时区的 UTC 时刻。

    浏览器控件给的是不带时区的本地墙钟时间，另带 JS
    getTimezoneOffset()（UTC - 本地，分钟）；两者相加即 UTC。
    若显式带偏移（ISO 串）则直接采用。裸时间缺省按 UTC。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("浸染时间不能为空")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("浸染时间格式无效")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        try:
            offset = int((offset_minutes or "").strip() or 0)
        except ValueError:
            offset = 0
        # 限制在合理范围，防止异常输入
        offset = max(-14 * 60, min(14 * 60, offset))
        dt = dt + timedelta(minutes=offset)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_lot_input(vat_pk: int, dipped_at_raw: str, offset_raw: str,
                     meters_raw: str, redox_raw: str) -> DipLotIn:
    """先完整解析、校验，再交给调用方入库：任何一项失败都不产生写操作。"""
    dipped_at = _parse_dipped_at(dipped_at_raw, offset_raw)
    meters = _parse_decimal(meters_raw, "布料米数")
    redox = _parse_optional_decimal(redox_raw, "氧化还原电位")
    try:
        return DipLotIn(
            vat_id=vat_pk,
            dippedAt=dipped_at,
            clothMeters=meters,
            redoxMv=redox,
        )
    except ValidationError as exc:
        msg = "；".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(msg)


def _workshop_id(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _iso_dt(dt: Optional[datetime]) -> Optional[str]:
    """下发带时区的 UTC ISO 时刻，由前端按浏览器本地时区展示。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ----------------------------- 展示数据 -----------------------------

def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空），按时间正序。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(db: Session, vat: Vat) -> dict:
    # 统一按带时区的真实时刻升序，时刻相同再按 id；最新在前
    chronological = vat.sorted_lots()
    latest = chronological[-1] if chronological else None
    recent = list(reversed(chronological[-8:]))
    total_lots = db.query(func.count(DipLot.id)).filter(DipLot.vat_id == vat.id).scalar()
    measured_lots = (
        db.query(func.count(DipLot.id))
        .filter(DipLot.vat_id == vat.id, DipLot.redoxMv.isnot(None))
        .scalar()
    )
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": _iso_dt(latest.dippedAt) if latest else None,
        "spark": _spark_points(chronological),
        "lotCount": total_lots,
        "measuredCount": measured_lots,
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": _iso_dt(l.dippedAt),
                "clothMeters": float(l.clothMeters),
                # 未测就是 null，前端不得伪造为 0 mV
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(db, v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


def _failure_page(request: Request, db: Session, user, ws, pk, message: str):
    """失败后整笔回滚，再用干净事务渲染：还原台必须打得开。"""
    db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, message),
        status_code=400,
    )


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = _workshop_id(workshop)
    if not item:
        return RedirectResponse("/", status_code=303)
    # 更新同样先校验：非法状态不落库
    if status not in Vat.STATUSES:
        return _failure_page(request, db, user, ws, pk, f"状态更新无效：未知缸状态 {status!r}")
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (VatRuleError, ValueError) as exc:
        message = exc.message if isinstance(exc, VatRuleError) else str(exc)
        return _failure_page(request, db, user, ws, pk, f"状态更新无效：{message}")


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(""),
    clothMeters: str = Form(""),
    redoxMv: str = Form(""),
    dippedAtOffset: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = _workshop_id(workshop)
    if not item:
        return RedirectResponse("/", status_code=303)
    try:
        # 全部字段先解析、校验通过后才开写：非法整笔不入库，不留残行
        lot_in = _build_lot_input(pk, dippedAt, dippedAtOffset, clothMeters, redoxMv)
    except ValueError as exc:
        return _failure_page(request, db, user, ws, pk, f"浸染记录无效：{exc}")
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=lot_in.dippedAt,  # 带时区 UTC
            clothMeters=lot_in.clothMeters,
            redoxMv=lot_in.redoxMv,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except Exception as exc:  # 数据库层约束等任何失败：回滚，绝不留残行
        return _failure_page(request, db, user, ws, pk, f"浸染记录无效：{exc}")


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
