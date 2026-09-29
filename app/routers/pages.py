from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop, APP_TZ, lot_aware_dt
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False, default=str))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}
VALID_STATUSES = frozenset(STATUS_LABELS)


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
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


def _fmt_dt(dt: datetime) -> str:
    """存储/排序按 UTC；展示按应用时区还原成本地墙钟。"""
    return lot_aware_dt(dt).astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M")


def _vat_payload(vat: Vat) -> dict:
    # 时刻统一按带时区归一化后排序：最新在前；同一时刻按 id 新者在前。
    lots = sorted(
        vat.lots,
        key=lambda x: (lot_aware_dt(x.dippedAt), x.id),
        reverse=True,
    )
    latest = lots[0] if lots else None
    recent = lots[:8]
    measured = [l for l in lots if l.redoxMv is not None]
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
        "lastDippedAt": _fmt_dt(latest.dippedAt) if latest else None,
        # 时间正序喂给 sparkline；只含真实读到的电位，不补零。
        "spark": _spark_points(list(reversed(lots))),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": _fmt_dt(l.dippedAt),
                "clothMeters": float(l.clothMeters),
                # 展示层不伪造电位：空读数保持 null，前端显式标「未测」。
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
        # 对账字段：近笔条数与库内非空电位笔数可核对。
        "lotCount": len(lots),
        "measuredCount": len(measured),
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
    payloads = [_vat_payload(v) for v in vats]
    # 失败回滚后也保证展开区有数据：选中缸不存在于本页数据时不挂空引用。
    if selected_vat is not None and not any(p["id"] == selected_vat for p in payloads):
        selected_vat = None
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": payloads,
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


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
    status: str = Form(...),
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
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        if status not in VALID_STATUSES:
            raise VatRuleError(f"未知缸状态：{status or '（空）'}")
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


def _parse_lot_form(dippedAt: str, clothMeters: str, redoxMv: str) -> tuple[datetime, Decimal, Optional[Decimal]]:
    """整笔先解析并校验，任何一项不合法即抛 ValueError —— 调用方必须不入库。"""
    dipped_at = dippedAt.strip()
    if not dipped_at:
        raise ValueError("浸染时间不能为空")
    try:
        dt = datetime.fromisoformat(dipped_at)
    except ValueError:
        raise ValueError("浸染时间格式无效")
    dt = lot_aware_dt(dt)

    meters_raw = clothMeters.strip().replace(",", "")
    if not meters_raw:
        raise ValueError("布米数不能为空")
    try:
        meters = Decimal(meters_raw)
    except InvalidOperation:
        raise ValueError("布米数不是有效数字")
    if not meters.is_finite() or meters <= 0:
        raise ValueError("布米须为正数")

    redox_raw = redoxMv.strip().replace(",", "")
    redox: Optional[Decimal] = None
    if redox_raw:
        try:
            redox = Decimal(redox_raw)
        except InvalidOperation:
            raise ValueError("氧化还原电位不是有效数字")
        if not redox.is_finite():
            raise ValueError("氧化还原电位不是有效数字")
    return dt, meters, redox


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(""),
    clothMeters: str = Form(""),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        # 先把整笔解析校验通过，再构造对象 —— 非法提交（含并发、连点）零残行。
        dt, meters, redox = _parse_lot_form(dippedAt, clothMeters, redoxMv)
        lot = DipLot(
            vat_id=pk,
            dippedAt=dt,
            clothMeters=meters,
            redoxMv=redox,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except ValueError as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    except Exception:
        # 任何意外（DB 错误/并发冲突等）都不允许留下半笔。
        db.rollback()
        raise
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
