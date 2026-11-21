"""去重换键核对的纯规则：不读写 store，服务层与示例数据共用同一套判定。

`evaluate_batch` 接收批次字典和事件列表，就地补齐每笔事件的旧键、新组合键、
键口径、两代分区与归属状态，并算出双键分区迁移差额。判定顺序固定为：

1. 事件标识 / 事件指纹去重：每笔事件只保留一个有效归属；
2. 旧批次范围：按旧键解释本次迁移范围；
3. 来源授权：跨来源修改分区直接拒绝并回退源端；
4. 键口径：只有同时带旧键和新组合键的事件进入换键核对；
5. 旧键压缩：同一旧键拆出多个新归属且分区要切换、旧批次未确认时整组冲突。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.dedup_keys import (
    compose_new_key,
    event_fingerprint,
    infer_key_scope,
    normalize_old_keys,
    normalize_sources,
    partition_of,
)

STATUS_PENDING = "待核对"
STATUS_READY = "待移交"
STATUS_CONFLICT = "归属冲突"
STATUS_HANDED = "已移交"
STATUS_INTERRUPTED = "已中断"
STATUS_DELIVERED = "已交付"
STATUS_CLEAN = "核对完成"

ACTION_RECONCILE = "开始核对"
ACTION_CONFIRM = "确认旧批次"
ACTION_HANDOVER = "移交差额"
ACTION_INTERRUPT = "中断迁移"
ACTION_RESUME = "继续迁移"
ACTION_DELIVER = "交付批次"
ACTIONS = [
    ACTION_RECONCILE, ACTION_CONFIRM, ACTION_HANDOVER,
    ACTION_INTERRUPT, ACTION_RESUME, ACTION_DELIVER,
]

BATCH_STATUSES = [
    STATUS_PENDING, STATUS_READY, STATUS_CONFLICT,
    STATUS_HANDED, STATUS_INTERRUPTED, STATUS_DELIVERED, STATUS_CLEAN,
]

EVENT_STATUS_PASS = "通过"
EVENT_STATUS_PENDING = "待核对"
EVENT_STATUS_REJECTED = "拒绝"
EVENT_STATUS_DUPLICATE = "重复"
EVENT_STATUS_CONFLICT = "冲突"
EVENT_STATUS_OUT_OF_SCOPE = "超出范围"
EVENT_STATUS_OLD_KEY_ONLY = "仅旧键"

# 核对过程会重算这些字段，重新核对前先清空，保证结果可重放。
COMPUTED_FIELDS = [
    "旧键", "新组合键", "键口径", "事件指纹", "旧分区", "目标分区",
    "归属状态", "有效归属", "冲突原因", "核对时间",
]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def evaluate_batch(batch: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """就地核对批次事件，返回摘要、差额与冲突计数。"""
    old_key_field = _text(batch.get("旧键字段"))
    new_key_spec = _text(batch.get("新组合键"))
    try:
        partition_count = int(batch.get("分区数量") or 10)
    except (TypeError, ValueError):
        partition_count = 10
    authorized = set(normalize_sources(batch.get("来源授权")))
    scope = set(normalize_old_keys(batch.get("旧批次范围")))
    confirmed = bool(batch.get("旧批次已确认"))

    seen_event_ids: dict[str, dict[str, Any]] = {}
    seen_fingerprints: dict[str, dict[str, Any]] = {}
    candidates_by_old_key: dict[str, list[dict[str, Any]]] = {}

    for event in events:
        for field in COMPUTED_FIELDS:
            event.pop(field, None)

        old_key = _text(event.get(old_key_field) if event.get(old_key_field) is not None else None)
        if not old_key:
            old_key = _text(event.get("旧键"))
        new_key = compose_new_key(event, new_key_spec)
        scope_name = infer_key_scope(event, new_key_spec, old_key_field)
        fingerprint = event_fingerprint(event)

        event["旧键"] = old_key
        event["新组合键"] = new_key
        event["键口径"] = scope_name
        event["事件指纹"] = fingerprint
        event["旧分区"] = partition_of(old_key, partition_count) if old_key else None
        event["目标分区"] = partition_of(new_key, partition_count) if new_key else None
        event["有效归属"] = False
        event["核对时间"] = _now()

        event_id = _text(event.get("事件标识"))
        source = _text(event.get("来源编号"))

        if event_id and event_id in seen_event_ids:
            first = seen_event_ids[event_id]
            if first.get("事件指纹") == fingerprint:
                event["归属状态"] = EVENT_STATUS_DUPLICATE
                event["冲突原因"] = "事件标识重复投递，完全重复不产生第二归属"
            else:
                event["归属状态"] = EVENT_STATUS_CONFLICT
                event["冲突原因"] = f"事件标识 {event_id} 被不同负载重复占用"
            continue
        # 内容完全相同的事件（无论事件标识是否不同）都视为重复投递，
        # 只有第一个到达的事件保留有效归属。
        if fingerprint in seen_fingerprints:
            event["归属状态"] = EVENT_STATUS_DUPLICATE
            event["冲突原因"] = "事件指纹相同，判定为完全重复，不产生第二归属"
            if event_id:
                seen_event_ids[event_id] = seen_fingerprints[fingerprint]
            continue

        if scope and old_key not in scope:
            event["归属状态"] = EVENT_STATUS_OUT_OF_SCOPE
            event["冲突原因"] = f"旧键 {old_key or '空'} 不在本批次旧批次范围内"
            continue

        if source not in authorized:
            event["归属状态"] = EVENT_STATUS_REJECTED
            event["冲突原因"] = (
                f"跨来源修改分区：来源 {source or '空'} 不在授权范围 {sorted(authorized)}")
            continue

        if scope_name == "旧键" or not new_key:
            event["归属状态"] = EVENT_STATUS_OLD_KEY_ONLY
            event["冲突原因"] = "事件仅携带旧键，不在组合键迁移范围"
            continue

        seen_event_ids[event_id] = event
        seen_fingerprints[fingerprint] = event
        event["归属状态"] = EVENT_STATUS_PENDING
        candidates_by_old_key.setdefault(old_key, []).append(event)

    conflicts = 0
    for old_key, group in candidates_by_old_key.items():
        distinct_new_keys = {_text(item.get("新组合键")) for item in group}
        moved = any(item.get("目标分区") != item.get("旧分区") for item in group)
        if len(distinct_new_keys) > 1 and moved and not confirmed:
            reason = (
                f"旧键 {old_key} 压缩了 {len(group)} 笔事件，新组合键将其拆分为 "
                f"{len(distinct_new_keys)} 个归属，分区已准备切换但旧批次尚未确认"
            )
            for item in group:
                item["归属状态"] = EVENT_STATUS_CONFLICT
                item["冲突原因"] = reason
                item["有效归属"] = False
                conflicts += 1
        else:
            for item in group:
                item["归属状态"] = EVENT_STATUS_PASS
                item["有效归属"] = True

    differences: list[dict[str, Any]] = []
    for event in events:
        if event.get("键口径") != "双键" or not event.get("旧键") or not event.get("新组合键"):
            continue
        if event.get("旧分区") == event.get("目标分区"):
            continue
        if event.get("归属状态") not in (
                EVENT_STATUS_PASS, EVENT_STATUS_CONFLICT, EVENT_STATUS_REJECTED):
            continue
        differences.append({
            "事件标识": _text(event.get("事件标识")),
            "旧键": _text(event.get("旧键")),
            "新组合键": _text(event.get("新组合键")),
            "键口径": _text(event.get("键口径")),
            "来源编号": _text(event.get("来源编号")),
            "旧分区": event.get("旧分区"),
            "目标分区": event.get("目标分区"),
            "归属状态": _text(event.get("归属状态")),
            "有效归属": bool(event.get("有效归属")),
            "冲突原因": _text(event.get("冲突原因")),
            "事件指纹": _text(event.get("事件指纹")),
        })

    counts = counts_of(events)
    summary = {
        **counts,
        "差额数": len(differences),
        "可接管数": sum(1 for item in differences if item["归属状态"] == EVENT_STATUS_PASS),
        "待确认数": sum(1 for item in differences if item["归属状态"] == EVENT_STATUS_CONFLICT),
        "回退源端数": sum(1 for item in differences if item["归属状态"] == EVENT_STATUS_REJECTED),
        "双键事件数": sum(1 for event in events if event.get("键口径") == "双键"),
        "分区切换就绪": any(item["归属状态"] != EVENT_STATUS_REJECTED for item in differences),
    }
    return {
        "counts": counts,
        "summary": summary,
        "differences": differences,
        "conflict_count": conflicts,
    }


def counts_of(events: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [_text(event.get("归属状态")) for event in events]
    return {
        "事件总数": len(events),
        "通过数": statuses.count(EVENT_STATUS_PASS),
        "拒绝数": statuses.count(EVENT_STATUS_REJECTED),
        "重复数": statuses.count(EVENT_STATUS_DUPLICATE),
        "冲突数": statuses.count(EVENT_STATUS_CONFLICT),
        "超出范围数": statuses.count(EVENT_STATUS_OUT_OF_SCOPE),
        "仅旧键数": statuses.count(EVENT_STATUS_OLD_KEY_ONLY),
        "有效归属数": sum(1 for event in events if event.get("有效归属")),
    }


def settle_status(result: dict[str, Any]) -> tuple[str, bool, str]:
    """核对后的批次状态、异常标记与迁移结论。"""
    if result["conflict_count"]:
        return (
            STATUS_CONFLICT, True,
            f"存在 {result['conflict_count']} 笔有效归属冲突，可先移交差额等待旧批次确认",
        )
    differences = result["differences"]
    if differences:
        return (
            STATUS_READY, False,
            f"核对完成，可移交 {len(differences)} 笔双键分区迁移差额",
        )
    return STATUS_CLEAN, False, "核对完成，没有需要迁移的分区差额"
