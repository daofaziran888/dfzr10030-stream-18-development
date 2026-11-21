"""去重换键迁移的示例批次：三条验证分组 + 一个已交付批次。

这里只生成纯数据，复用 `dedup_migration_rules.evaluate_batch` 的同一套判定，
所以起服务后看到的归属状态、分区差额与接口核对结果完全一致。
"""
from __future__ import annotations

from typing import Any

from app.services.dedup_migration_rules import (
    STATUS_CONFLICT,
    STATUS_DELIVERED,
    STATUS_READY,
    evaluate_batch,
)


def _batch(
    batch_id: int,
    code: str,
    *,
    scope: list[str],
    sources: list[str],
    confirmed: bool,
    note: str,
) -> dict[str, Any]:
    return {
        "id": batch_id,
        "批次编号": code,
        "关联策略": "DEDU-0002",
        "关联流": "订单事件流",
        "旧键字段": "订单编号",
        "新组合键": "订单编号|来源编号|业务身份",
        "分区数量": 10,
        "旧批次范围": scope,
        "来源授权": sources,
        "旧批次已确认": confirmed,
        "状态": "待核对",
        "pending": True,
        "abnormal": False,
        "创建时间": "2026-11-20T09:00:00",
        "更新时间": "2026-11-20T09:00:00",
        "核对时间": None,
        "移交时间": None,
        "事件总数": 0,
        "核对摘要": {},
        "迁移差额": [],
        "接管结论": [],
        "迁移结论": note,
        "备注": note,
    }


def _event(
    event_id: str,
    batch_id: int,
    code: str,
    order: str,
    source: str,
    identity: str,
    amount: float,
) -> dict[str, Any]:
    return {
        "事件标识": event_id,
        "批次id": batch_id,
        "迁移编号": code,
        "订单编号": order,
        "来源编号": source,
        "业务身份": identity,
        "金额": amount,
        "event_time": "2026-11-20T09:00:00Z",
        "登记时间": "2026-11-20T09:00:00",
    }


def build_seed_migrations() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    batches = [
        _batch(1, "MIG-DEDUP-01", scope=["ORD-1001"], sources=["SOUR-A"],
               confirmed=False, note="同旧键不同来源：跨来源修改分区需拒绝"),
        _batch(2, "MIG-DEDUP-02", scope=["ORD-2001"], sources=["SOUR-A"],
               confirmed=False, note="同旧键同来源不同业务身份：旧批次未确认前整组冲突"),
        _batch(3, "MIG-DEDUP-03", scope=["ORD-3001"], sources=["SOUR-A"],
               confirmed=False, note="已交付批次：只读回查，按旧键解释本次迁移范围"),
    ]
    events = [
        # 分组一：同旧键、不同来源。SOUR-B 的改分区请求越权，退回源端；
        # 其余同内容投递属于完全重复，只保留一条有效归属。
        _event("EV-M1-001", 1, "MIG-DEDUP-01", "ORD-1001", "SOUR-A", "BIZ-01", 100.0),
        _event("EV-M1-002", 1, "MIG-DEDUP-01", "ORD-1001", "SOUR-B", "BIZ-01", 100.0),
        _event("EV-M1-003", 1, "MIG-DEDUP-01", "ORD-1001", "SOUR-A", "BIZ-01", 100.0),
        _event("EV-M1-001", 1, "MIG-DEDUP-01", "ORD-1001", "SOUR-A", "BIZ-01", 100.0),
        # 分组二：同旧键、同来源，业务身份不同。旧键压成两笔、新组合键拆开，
        # 分区要切换且旧批次未确认，整组构成有效归属冲突。
        _event("EV-M2-001", 2, "MIG-DEDUP-02", "ORD-2001", "SOUR-A", "BIZ-02", 200.0),
        _event("EV-M2-002", 2, "MIG-DEDUP-02", "ORD-2001", "SOUR-A", "BIZ-03", 300.0),
        # 分组三：已交付批次。旧批次已确认，冲突解除，差额已移交并交付。
        _event("EV-M3-001", 3, "MIG-DEDUP-03", "ORD-3001", "SOUR-A", "BIZ-04", 400.0),
        _event("EV-M3-002", 3, "MIG-DEDUP-03", "ORD-3001", "SOUR-A", "BIZ-05", 500.0),
    ]
    for index, event in enumerate(events, start=1):
        event["id"] = index

    by_batch: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
    for event in events:
        by_batch[int(event["批次id"])].append(event)

    final_states = {
        1: (STATUS_READY, False, None, None),
        2: (STATUS_CONFLICT, True, None, None),
        3: (STATUS_DELIVERED, False, "2026-11-20T09:08:00", "2026-11-20T09:10:00"),
    }
    for batch in batches:
        batch_events = by_batch[int(batch["id"])]
        # 已交付的示例批次代表"旧批次已确认"之后的终态，按同一套规则重算。
        if int(batch["id"]) == 3:
            batch["旧批次已确认"] = True
        result = evaluate_batch(batch, batch_events)
        status, abnormal, handed_at, updated_at = final_states[int(batch["id"])]
        if status == STATUS_CONFLICT:
            conclusion = f"存在 {result['conflict_count']} 笔有效归属冲突，可先移交差额等待旧批次确认"
        elif status == STATUS_READY:
            conclusion = f"核对完成，可移交 {len(result['differences'])} 笔双键分区迁移差额"
        else:
            conclusion = "批次已交付，仅保留按旧键的只读回查能力"
        batch.update({
            "状态": status,
            "abnormal": abnormal,
            "pending": False,
            "核对时间": "2026-11-20T09:05:00",
            "移交时间": handed_at,
            "更新时间": updated_at or "2026-11-20T09:05:00",
            "事件总数": len(batch_events),
            "核对摘要": result["summary"],
            "迁移差额": result["differences"],
            "迁移结论": conclusion,
        })
    return batches, events


SEED_MIGRATION_BATCHES, SEED_MIGRATION_EVENTS = build_seed_migrations()
