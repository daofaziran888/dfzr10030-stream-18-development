"""分组分区侧的分区迁移接管：消费去重差额、按事件标识关联目标归属、回写切换清单。

分组分区只处理「同时带旧订单键和新组合键、且旧分区与目标分区不同」的差额：

* 目标分区必须落在本策略的分区数量范围内，否则按越界退回源端；
* 来源编号必须落在分组策略的来源授权内，跨来源接管一律拒绝并退回源端；
* 一笔事件标识只能有一个有效归属，重复差额只认第一条，其余标记冲突；
* 有效归属冲突的记录不得接管，等旧批次确认后重新消费；
* 接管结论按事件标识回写去重批次的迁移结论与切换清单，已交付批次只读不改。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.dedup_keys import normalize_sources
from app.services.dedup_migration import (
    DedupMigrationError,
    service as dedup_migration_service,
)
from app.store import store

MODULE = "grp_partition"
SWITCH_TABLE = "_partition_switch"

DECISION_TAKEN_OVER = "已接管"
DECISION_RETURNED = "退回源端"
DECISION_CONFLICT = "归属冲突"
DECISION_PENDING = "待确认"

CONCLUSION_MERGED = "已并入切换清单"
CONCLUSION_BLOCKED = "待旧批次确认"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class GrpPartitionMigrationError(ValueError):
    """接管动作不满足前置条件时抛出，路由层转成可读的 ok=False。"""


class PartitionHandoverService:
    def list_switch_entries(
        self,
        *,
        batch_code: str | None = None,
        decision: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(SWITCH_TABLE)
        if batch_code:
            rows = [row for row in rows if _text(row.get("批次编号")) == _text(batch_code)]
        if decision:
            rows = [row for row in rows if _text(row.get("接管结论")) == _text(decision)]
        total = len(rows)
        start = (page - 1) * size
        return [dict(row) for row in rows[start:start + size]], total

    def switch_entries(self, batch_code: str | None = None) -> list[dict[str, Any]]:
        return self.list_switch_entries(batch_code=batch_code, page=1, size=1_000_000)[0]

    def consume(self, batch_id: int, group_id: int) -> dict[str, Any]:
        """消费一个已移交批次的双键分区迁移差额，按事件标识逐条给出接管结论。"""
        group = store.find(MODULE, group_id)
        if group is None:
            raise GrpPartitionMigrationError(f"分组策略 {group_id} 不存在或已归档")
        if _text(group.get("status")) == "已停用":
            raise GrpPartitionMigrationError(f"分组策略 {group_id} 已停用，不能接管分区迁移")

        try:
            differences = dedup_migration_service.get_differences(batch_id)
        except DedupMigrationError as error:
            raise GrpPartitionMigrationError(str(error)) from error
        batch = dedup_migration_service.get_batch(batch_id)
        if batch is None:
            raise GrpPartitionMigrationError(f"迁移批次 {batch_id} 不存在或已归档")

        authorized = set(normalize_sources(group.get("来源授权")))
        if not authorized:
            # 策略未声明来源授权时，退化为采用去重批次自身的来源授权口径。
            authorized = set(normalize_sources(batch.get("来源授权")))
        try:
            partition_count = int(group.get("分区数量") or batch.get("分区数量") or 10)
        except (TypeError, ValueError):
            partition_count = 10

        batch_code = _text(batch.get("批次编号"))
        switch_rows = store.rows(SWITCH_TABLE)
        existing = {
            (_text(row.get("批次编号")), _text(row.get("事件标识"))): row
            for row in switch_rows
        }
        claimed_by_event: dict[str, dict[str, Any]] = {}
        conclusions: list[dict[str, Any]] = []
        taken = returned = conflicted = pending = 0

        for item in differences:
            event_id = _text(item.get("事件标识"))
            decision, reason, ownership = self._decide(
                item, authorized, partition_count, claimed_by_event)
            if decision == DECISION_TAKEN_OVER:
                taken += 1
                claimed_by_event[event_id] = item
                ownership = True
            elif decision == DECISION_RETURNED:
                returned += 1
                ownership = False
            elif decision == DECISION_CONFLICT:
                conflicted += 1
                ownership = False
            else:
                pending += 1
                ownership = False

            conclusion = {
                "批次编号": batch_code,
                "批次id": batch_id,
                "事件标识": event_id,
                "旧键": _text(item.get("旧键")),
                "新组合键": _text(item.get("新组合键")),
                "旧分区": item.get("旧分区"),
                "目标分区": item.get("目标分区"),
                "来源编号": _text(item.get("来源编号")),
                "分组策略id": group_id,
                "分组策略编号": _text(group.get("策略编号")),
                "接管结论": decision,
                "目标归属": ownership,
                "结论说明": reason,
                "去重归属状态": _text(item.get("归属状态")),
                "回写时间": _now(),
            }
            conclusions.append(conclusion)

            # 切换清单按 (批次编号, 事件标识) 幂等更新，重复消费不会产生第二条归属。
            key = (batch_code, event_id)
            row = existing.get(key)
            if row is None:
                row = {"id": max((int(r.get("id", 0)) for r in switch_rows), default=0) + 1}
                row.update(conclusion)
                switch_rows.append(row)
                existing[key] = row
            else:
                row.update(conclusion)

        group["最近接管"] = {
            "批次编号": batch_code,
            "接管数": taken,
            "退回数": returned,
            "冲突数": conflicted,
            "待确认数": pending,
            "回写时间": _now(),
            "左右迁移": f"左分区 {sorted({c['旧分区'] for c in conclusions if c['旧分区'] is not None})}"
                        f" -> 右分区 {sorted({c['目标分区'] for c in conclusions if c['目标分区'] is not None})}",
        }
        group["左右迁移"] = group["最近接管"]["左右迁移"]

        self._write_back(batch_id, conclusions, taken, returned, conflicted, pending)
        return {
            "批次编号": batch_code,
            "分组策略编号": _text(group.get("策略编号")),
            "差额数": len(differences),
            "接管数": taken,
            "退回源端数": returned,
            "归属冲突数": conflicted,
            "待确认数": pending,
            "切换清单": conclusions,
        }

    def _decide(
        self,
        item: dict[str, Any],
        authorized: set[str],
        partition_count: int,
        claimed_by_event: dict[str, dict[str, Any]],
    ) -> tuple[str, str, bool]:
        """单笔差额的接管判定，返回 (结论, 说明, 目标归属)。"""
        event_id = _text(item.get("事件标识"))
        source = _text(item.get("来源编号"))
        target = item.get("目标分区")
        old = item.get("旧分区")

        if _text(item.get("归属状态")) == "冲突" or not item.get("有效归属"):
            if _text(item.get("归属状态")) == "冲突":
                return (
                    DECISION_PENDING,
                    "去重侧判定有效归属冲突，旧批次确认前不接管",
                    False,
                )
            return (
                DECISION_RETURNED,
                _text(item.get("冲突原因")) or "去重侧未给出有效归属，退回源端",
                False,
            )
        if source not in authorized:
            return (
                DECISION_RETURNED,
                f"跨来源修改分区：来源 {source or '空'} 不在分组策略授权范围 {sorted(authorized)}",
                False,
            )
        if not isinstance(target, int) or target < 0 or target >= partition_count:
            return (
                DECISION_RETURNED,
                f"目标分区 {target} 超出分组策略分区数量 {partition_count}",
                False,
            )
        if event_id and event_id in claimed_by_event:
            return (
                DECISION_CONFLICT,
                f"事件标识 {event_id} 已有有效归属，不能重复接管",
                False,
            )
        if old == target:
            return (
                DECISION_RETURNED,
                "新旧分区相同，无需迁移",
                False,
            )
        if not event_id:
            return (
                DECISION_RETURNED,
                "缺少事件标识，无法关联目标归属，退回源端",
                False,
            )
        return DECISION_TAKEN_OVER, f"按事件标识 {event_id} 关联到目标分区 {target}", True

    def _write_back(
        self,
        batch_id: int,
        conclusions: list[dict[str, Any]],
        taken: int,
        returned: int,
        conflicted: int,
        pending: int,
    ) -> None:
        """把接管结论回写去重批次的迁移结论；已交付批次完全只读。"""
        batch = store.find("_dedup_migrations", batch_id)
        if batch is None:
            return
        if _text(batch.get("状态")) == "已交付":
            # 已交付批次只读：接管结论只进切换清单，不回写批次自身。
            return
        batch["接管结论"] = conclusions
        batch["更新时间"] = _now()
        batch["迁移结论"] = (
            f"分组分区已消费 {len(conclusions)} 笔差额：接管 {taken} 笔、退回源端 {returned} 笔、"
            f"归属冲突 {conflicted} 笔、待确认 {pending} 笔"
        )
        if pending:
            batch["abnormal"] = True
        elif batch.get("状态") == "已移交":
            batch["abnormal"] = False


handover_service = PartitionHandoverService()
