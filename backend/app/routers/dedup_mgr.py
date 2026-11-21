"""去重管理接口：维护去重策略，覆盖启用去重、停用去重、清理状态等动作。

另含去重换键迁移：批次登记、事件核对、双键差额移交、只读回查与两代键对账。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.dedup_migration import (
    BATCH_STATUSES,
    DedupMigrationError,
    service as migration_service,
)
from app.services.dedup_mgr import DedupMgrService

router = APIRouter(prefix="/api/dedup_mgr", tags=["去重管理"])

service = DedupMgrService()

LIST_FIELDS = ["策略编号", "关联流", "去重键", "去重窗口", "保留策略", "状态上限", "命中次数", "误杀率"]
STATUSES = ["草稿", "运行中", "容量告警", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、容量告警、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、策略编号关键字与状态过滤去重管理列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dedup_mgr.csv"'})


# ---------------------------------------------------------------------------
# 去重换键迁移：路由必须先声明固定路径，再声明 /{entry_id} 之类的动态路径。
# ---------------------------------------------------------------------------
MIGRATION_LIST_FIELDS = ["批次编号", "关联策略", "关联流", "状态"]


@router.get("/migrations", response_model=PageResult[dict])
def list_migrations(
    request: Request,
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="批次状态"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """列出迁移批次；批次编号关键字与状态过滤与列表口径一致。"""
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in MIGRATION_LIST_FIELDS and value.strip()
    }
    items, total = migration_service.list_batches(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/migrations", response_model=ActionResult)
def create_migration(payload: dict[str, object]) -> ActionResult:
    """登记迁移批次：批次编号、关联策略、旧键字段、新组合键、来源授权都是必填。"""
    batch, errors = migration_service.create_batch(payload)
    if batch is None:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="迁移批次已登记", entry=batch)


@router.get("/migrations/{batch_id}", response_model=dict)
def get_migration(batch_id: int) -> dict:
    """读取批次明细：摘要、迁移差额与逐条事件台账。"""
    detail = migration_service.get_batch_detail(batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"迁移批次 {batch_id} 不存在或已归档")
    return detail


@router.get("/migrations/{batch_id}/legacy")
def get_migration_legacy(batch_id: int, old_key: str | None = Query(default=None)) -> dict:
    """已交付批次的只读回查：按旧键解释本次迁移范围，并给出两代键对账。"""
    try:
        return migration_service.legacy_view(batch_id, old_key=old_key)
    except DedupMigrationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/migrations/{batch_id}/reconciliation")
def get_migration_reconciliation(batch_id: int, old_key: str | None = Query(default=None)) -> dict:
    """迁移中断后也能用的两代键对账：按旧键汇总新旧归属与数据去向。"""
    try:
        return migration_service.reconciliation_view(batch_id, old_key=old_key)
    except DedupMigrationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/migrations/{batch_id}/events", response_model=ActionResult)
def ingest_migration_events(batch_id: int, payload: dict[str, object]) -> ActionResult:
    """登记需要核对的事件；events 支持单条对象或数组，重复登记会重置为待核对。"""
    raw = payload.get("events", payload)
    if isinstance(raw, dict):
        events = [raw]
    elif isinstance(raw, list):
        events = [item for item in raw if isinstance(item, dict)]
    else:
        return ActionResult(ok=False, message="events 必须是事件对象或数组")
    try:
        result = migration_service.ingest_events(batch_id, events)
    except DedupMigrationError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message="迁移事件已登记，等待核对", entry=result)


@router.post("/migrations/{batch_id}/actions", response_model=ActionResult)
def run_migration_action(batch_id: int, payload: ActionPayload) -> ActionResult:
    """执行核对、确认旧批次、移交差额、中断/继续与交付；已交付批次只读。"""
    detail, message = migration_service.run_action(batch_id, payload.action.strip())
    if detail is None:
        return ActionResult(ok=False, message=message)
    if isinstance(detail, dict) and "events" in detail:
        return ActionResult(ok=True, message=message, entry=detail)
    return ActionResult(ok=True, message=message, entry=detail)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条去重策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条去重策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="去重策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条去重策略执行启用去重、停用去重、清理状态；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
