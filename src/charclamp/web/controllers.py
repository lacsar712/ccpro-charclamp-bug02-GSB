from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, User, utcnow
from charclamp.domain.rules import (
    RuleError,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    parse_peak_temp,
)
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
    async with SessionLocal() as db:
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
    }


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list((await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all())
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        latest = None
        if clamp.shifts:
            latest = max(clamp.shifts, key=lambda s: s.started_at)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "latest_shift": latest,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(data.get("clamp_id"))
        if clamp_id is None:
            _set_flash(request, "班次登记失败：未选择有效炭窑", "error")
            return Redirect("/")
        try:
            peak = parse_peak_temp(data.get("peak_temp_c"))
            started_raw = (data.get("started_at") or "").strip()
            started_at = datetime.fromisoformat(started_raw) if started_raw else utcnow()
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
        except (RuleError, ValueError) as exc:
            _set_flash(request, f"班次登记失败：{exc}", "error")
            return Redirect(f"/?clamp_id={clamp_id}")
        async with SessionLocal() as db:
            shift = BurnShift(
                clamp_id=clamp_id,
                started_at=started_at,
                peak_temp_c=peak,
                charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                notes=(data.get("notes") or "").strip(),
            )
            db.add(shift)
            clamp = (
                await db.execute(select(Clamp).where(Clamp.id == clamp_id))
            ).scalar_one_or_none()
            if not clamp:
                _set_flash(request, "班次登记失败：炭窑不存在", "error")
                return Redirect("/")
            if clamp.status == Clamp.STATUS_STACKED:
                clamp.status = Clamp.STATUS_BURNING
            await db.commit()
        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")

    @post("/{shift_id:int}/peak")
    async def update_peak(
        self,
        request: Request,
        shift_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        """改最近一班峰值：正数校验 + row_version 乐观锁，失败走 PRG 回时间轴。"""
        if not request.user:
            return Redirect("/login")
        raw_version = (data.get("row_version") or "").strip()
        try:
            expected_version = int(raw_version)
        except (TypeError, ValueError):
            expected_version = None
        try:
            peak = parse_peak_temp(data.get("peak_temp_c"))
        except RuleError as exc:
            # 非法输入不能白板：flash 后重定向，时间轴重新加载完整剪影与卡片
            _set_flash(request, f"峰值保存失败：{exc}", "error")
            clamp_id = _parse_optional_int(data.get("clamp_id"))
            return Redirect(f"/?clamp_id={clamp_id}" if clamp_id is not None else "/")

        async with SessionLocal() as db:
            current = (
                await db.execute(
                    select(BurnShift.clamp_id, BurnShift.row_version).where(
                        BurnShift.id == shift_id
                    )
                )
            ).first()
            if current is None:
                _set_flash(request, "峰值保存失败：班次不存在或已被删除", "error")
                return Redirect("/")
            clamp_id, current_version = current

            if expected_version is None or expected_version != current_version:
                # 连点的第二个请求 / 拿旧表单的重复提交：版本已前进，拒绝覆盖
                _set_flash(
                    request,
                    "该班次峰值已被其他提交更新过，本次未写入；请刷新后查看最新值",
                    "error",
                )
                return Redirect(f"/?clamp_id={clamp_id}")

            # 条件更新：只有版本仍匹配时才写入并自增；
            # 与并发请求竞争时至多一个 rowcount=1，其余自动落空
            result = await db.execute(
                update(BurnShift)
                .where(BurnShift.id == shift_id, BurnShift.row_version == expected_version)
                .values(peak_temp_c=peak, row_version=BurnShift.row_version + 1)
            )
            await db.commit()
            if result.rowcount != 1:
                _set_flash(
                    request,
                    "该班次峰值已被其他提交更新过，本次未写入；请刷新后查看最新值",
                    "error",
                )
                return Redirect(f"/?clamp_id={clamp_id}")
        _set_flash(request, "峰值已保存", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")


class ClampController(Controller):

    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")
