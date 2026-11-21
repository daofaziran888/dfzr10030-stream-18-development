"""去重策略换键迁移：核对双键事件、识别归属冲突、向分组分区交出分区差额。

迁移批次围绕一次「旧键 -> 新组合键」的换键展开：

* 每条事件先按批次声明的旧键字段与新组合键定义算出旧键、新组合键、键口径；
* 来源编号必须落在批次来源授权内，否则按「跨来源修改分区」拒绝并回退源端；
* 同一事件标识 / 同一事件指纹只保留一个有效归属，重复投递不再产生第二归属；
* 旧键把多笔事件压成一笔、新组合键把它们分开，且分区已准备切换而旧批次尚未
  确认时，整组标记为有效归属冲突；
* 核对结果里的双键分区迁移差额（旧分区 != 目标分区）交给分组分区消费；
* 已交付批次冻结为只读，可按旧键回查台账与迁移范围。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.dedup_keys import normalize_old_keys, normalize_sources
from app.services.dedup_migration_rules import (
    ACTION_CONFIRM,
    ACTION_DELIVER,
    ACTION_HANDOVER,
    ACTION_INTERRUPT,
    ACTION_RECONCILE,
    ACTION_RESUME,
    ACTIONS,
    BATCH_STATUSES,
    COMPUTED_FIELDS,
    EVENT_STATUS_CONFLICT,
    EVENT_STATUS_DUPLICATE,
    EVENT_STATUS_PENDING,
    EVENT_STATUS_REJECTED,
    STATUS_CLEAN,
    STATUS_CONFLICT,
    STATUS_DELIVERED,
    STATUS_HANDED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_READY,
    evaluate_batch,
    settle_status,
)
from app.store import store

__all__ = [
    "BATCH_TABLE", "EVENT_TABLE", "ACTIONS", "BATCH_STATUSES", "STATUS_PENDING",
    "STATUS_READY", "STATUS_CONFLICT", "STATUS_HANDED", "STATUS_INTERRUPTED",
    "STATUS_DELIVERED", "STATUS_CLEAN", "DedupMigrationError", "DedupMigrationService",
    "service",
]

BATCH_TABLE = "_dedup_migrations"
EVENT_TABLE = "_migration_events"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _event_batch_id(event: dict[str, Any]) -> int | None:
    try:
        return int(event.get("批次id"))
    except (TypeError, ValueError):
        return None


class DedupMigrationError(ValueError):
    """迁移动作不满足前置条件时抛出，路由层转成可读的 ok=False。"""


class DedupMigrationService:
    # ------------------------------------------------------------------ 批次
    def list_batches(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(BATCH_TABLE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in _text(row.get(field)).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in _text(row.get("批次编号")).casefold()]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self._batch_view(row) for row in rows[start:start + size]], total

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        row = store.find(BATCH_TABLE, batch_id)
        return None if row is None else self._batch_view(row)

    def get_batch_detail(self, batch_id: int) -> dict[str, Any] | None:
        row = store.find(BATCH_TABLE, batch_id)
        if row is None:
            return None
        detail = self._batch_view(row)
        detail["events"] = self.ledger(batch_id)
        detail["摘要"] = dict(row.get("核对摘要") or {})
        detail["迁移差额"] = [dict(item) for item in row.get("迁移差额") or []]
        return detail

    def create_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记迁移批次；返回 (批次, 错误说明列表)。错误说明可直接展示给操作人。"""
        # 来源授权允许传数组，先按统一口径判断是否为空再报缺字段。
        required = {
            "批次编号": _text(values.get("批次编号")),
            "关联策略": _text(values.get("关联策略")),
            "旧键字段": _text(values.get("旧键字段")),
            "新组合键": _text(values.get("新组合键")),
            "来源授权": normalize_sources(values.get("来源授权")),
        }
        missing = [field for field, value in required.items() if not value]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        if self.find_by_code(_text(values.get("批次编号"))) is not None:
            return None, [f"批次编号 {required['批次编号']} 已存在，请换一个编号"]
        try:
            partition_count = int(values.get("分区数量") or 10)
        except (TypeError, ValueError):
            return None, ["分区数量必须是整数"]
        if partition_count <= 0:
            return None, ["分区数量必须大于 0"]

        rows = store.rows(BATCH_TABLE)
        batch = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": _text(values.get("批次编号")),
            "关联策略": _text(values.get("关联策略")),
            "关联流": _text(values.get("关联流")),
            "旧键字段": _text(values.get("旧键字段")),
            "新组合键": _text(values.get("新组合键")),
            "分区数量": partition_count,
            "旧批次范围": normalize_old_keys(values.get("旧批次范围")),
            "来源授权": normalize_sources(values.get("来源授权")),
            "旧批次已确认": bool(values.get("旧批次已确认")),
            "状态": STATUS_PENDING,
            "pending": True,
            "abnormal": False,
            "创建时间": _now(),
            "更新时间": _now(),
            "核对时间": None,
            "移交时间": None,
            "事件总数": 0,
            "核对摘要": {},
            "迁移差额": [],
            "接管结论": [],
            "迁移结论": "",
            "备注": _text(values.get("备注")),
        }
        rows.append(batch)
        return self._batch_view(batch), []

    def find_by_code(self, batch_code: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if _text(row.get("批次编号")) == _text(batch_code):
                return row
        return None

    # ------------------------------------------------------------------ 事件
    def ingest_events(self, batch_id: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        self._guard_mutable(batch)
        if not events:
            raise DedupMigrationError("未提供需要登记的事件")
        rows = store.rows(EVENT_TABLE)
        for raw in events:
            event = {key: value for key, value in raw.items() if key != "id"}
            event.update({
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "批次id": batch_id,
                "迁移编号": _text(batch.get("批次编号")),
                "登记时间": _now(),
            })
            rows.append(event)
        self._invalidate(batch, event_count=len(self.ledger(batch_id)))
        return {"批次id": batch_id, "登记事件数": len(events), "状态": batch["状态"]}

    def ledger(
        self,
        batch_id: int,
        *,
        old_key: str | None = None,
        event_status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_batch(batch_id)
        rows = [row for row in store.rows(EVENT_TABLE) if _event_batch_id(row) == batch_id]
        if old_key:
            expected = _text(old_key)
            rows = [row for row in rows if _text(row.get("旧键")) == expected
                    or _text(row.get("订单编号")) == expected]
        if event_status:
            rows = [row for row in rows if _text(row.get("归属状态")) == event_status]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 核对
    def reconcile(self, batch_id: int) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        self._guard_mutable(batch)
        events = [row for row in store.rows(EVENT_TABLE) if _event_batch_id(row) == batch_id]
        if not events:
            raise DedupMigrationError("批次尚未登记任何事件，无法核对")

        result = evaluate_batch(batch, events)
        status, abnormal, conclusion = settle_status(result)
        # 已移交过的批次重新核对后仍然保持「已移交」，分组分区可直接重算差额。
        if batch.get("移交时间") and status == STATUS_READY:
            status = STATUS_HANDED
            conclusion = f"核对完成，已移交的 {len(result['differences'])} 笔差额可重新消费"

        batch["核对摘要"] = result["summary"]
        batch["迁移差额"] = result["differences"]
        batch["事件总数"] = len(events)
        batch["核对时间"] = _now()
        batch["更新时间"] = _now()
        batch["pending"] = False
        batch["状态"] = status
        batch["abnormal"] = abnormal
        batch["迁移结论"] = conclusion
        return self.get_batch_detail(batch_id) or {}

    # ------------------------------------------------------------------ 动作
    def run_action(self, batch_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None, f"迁移批次 {batch_id} 不存在或已归档"
        if action not in ACTIONS:
            return None, f"动作「{action}」不属于去重换键迁移可执行范围"
        try:
            if action == ACTION_DELIVER:
                self._guard_mutable(batch)
                if batch.get("状态") not in (STATUS_HANDED, STATUS_CLEAN):
                    return None, "批次尚未移交差额，不能直接交付"
                batch["状态"] = STATUS_DELIVERED
                batch["pending"] = False
                batch["abnormal"] = False
                batch["更新时间"] = _now()
                batch["迁移结论"] = "批次已交付，仅保留按旧键的只读回查能力"
                return self._batch_view(batch), "迁移批次已交付，台账转为只读"
            if action == ACTION_CONFIRM:
                self._guard_mutable(batch)
                if batch.get("旧批次已确认"):
                    return self._batch_view(batch), "旧批次此前已确认，无需重复确认"
                batch["旧批次已确认"] = True
                batch["更新时间"] = _now()
                if batch.get("核对时间"):
                    self.reconcile(batch_id)
                return self.get_batch_detail(batch_id), "旧批次已确认，有效归属冲突按新组合键重新核对"
            if action == ACTION_INTERRUPT:
                self._guard_mutable(batch)
                # 中断不动已核对台账：保留旧键/新键两代去向，方便恢复后对账。
                batch["状态"] = STATUS_INTERRUPTED
                batch["pending"] = True
                batch["abnormal"] = True
                batch["更新时间"] = _now()
                batch["迁移结论"] = "迁移已中断，已处理台账保留，可按旧键对账两代键去向"
                return self._batch_view(batch), "迁移已中断，台账保留供两代键对账"
            if action in (ACTION_RECONCILE, ACTION_RESUME):
                self._guard_mutable(batch)
                detail = self.reconcile(batch_id)
                return detail, "换键核对已完成"
            if action == ACTION_HANDOVER:
                self._guard_mutable(batch)
                if not batch.get("核对时间"):
                    return None, "批次尚未核对，无法移交双键分区迁移差额"
                differences = list(batch.get("迁移差额") or [])
                if not differences:
                    return None, "当前批次没有可移交的双键分区迁移差额"
                conflicts = sum(1 for item in differences if item["归属状态"] == EVENT_STATUS_CONFLICT)
                # 有冲突时保留「归属冲突」状态，但照样移交：分区已准备切换，
                # 分组分区先把可接管的接管、待确认的挂起，等旧批次确认后重算。
                if not conflicts:
                    batch["状态"] = STATUS_HANDED
                batch["pending"] = False
                batch["移交时间"] = _now()
                batch["更新时间"] = _now()
                batch["迁移结论"] = (
                    f"已向分组分区移交 {len(differences)} 笔双键分区迁移差额"
                    + (f"，其中冲突 {conflicts} 笔待旧批次确认" if conflicts else "")
                )
                return self.get_batch_detail(batch_id), "双键分区迁移差额已移交分组分区"
        except DedupMigrationError as error:
            return None, str(error)
        return None, f"动作「{action}」未产生任何变更"

    # ------------------------------------------------------------------ 只读回查
    def legacy_view(self, batch_id: int, old_key: str | None = None) -> dict[str, Any]:
        """已交付（或任意状态）批次的只读回查：范围按旧键解释。"""
        batch = self._require_batch(batch_id)
        scope = normalize_old_keys(batch.get("旧批次范围"))
        events = [
            event for event in self.ledger(batch_id)
            if not old_key or _text(event.get("旧键")) == _text(old_key)
        ]
        return {
            "批次id": batch_id,
            "批次编号": _text(batch.get("批次编号")),
            "状态": _text(batch.get("状态")),
            "只读": batch.get("状态") == STATUS_DELIVERED,
            "旧键字段": _text(batch.get("旧键字段")),
            "新组合键": _text(batch.get("新组合键")),
            "旧批次范围": scope,
            "本次回查旧键": _text(old_key),
            "范围说明": (
                f"按旧键 {old_key} 解释本次迁移范围，仅回查该旧键下的事件"
                if old_key else "未指定旧键，回查批次覆盖的全部旧键"
            ),
            "事件": events,
            "两代键对账": self.reconciliation_view(batch_id, old_key=old_key)["分组"],
        }

    def reconciliation_view(self, batch_id: int, old_key: str | None = None) -> dict[str, Any]:
        """按旧键汇总两代键下的数据去向，迁移中断后同样可用。"""
        batch = self._require_batch(batch_id)
        scope = normalize_old_keys(batch.get("旧批次范围"))
        events = [
            event for event in self.ledger(batch_id)
            if not old_key or _text(event.get("旧键")) == _text(old_key)
        ]

        grouped: dict[str, dict[str, Any]] = {}
        for event in events:
            key = _text(event.get("旧键"))
            group = grouped.setdefault(key, {
                "旧键": key,
                "旧分区": event.get("旧分区"),
                "事件数": 0,
                "有效归属数": 0,
                "新组合键": {},
                "归属状态": {},
                "拒绝": [],
                "重复": [],
                "冲突": [],
                "数据去向": [],
            })
            group["事件数"] += 1
            status = _text(event.get("归属状态")) or EVENT_STATUS_PENDING
            group["归属状态"][status] = group["归属状态"].get(status, 0) + 1
            if event.get("有效归属"):
                group["有效归属数"] += 1
            new_key = _text(event.get("新组合键"))
            if new_key:
                target = group["新组合键"].setdefault(new_key, {
                    "目标分区": event.get("目标分区"),
                    "事件标识": [],
                    "有效归属": False,
                })
                target["事件标识"].append(_text(event.get("事件标识")))
                target["有效归属"] = target["有效归属"] or bool(event.get("有效归属"))
            if status == EVENT_STATUS_REJECTED:
                group["拒绝"].append(_text(event.get("事件标识")))
            elif status == EVENT_STATUS_DUPLICATE:
                group["重复"].append(_text(event.get("事件标识")))
            elif status == EVENT_STATUS_CONFLICT:
                group["冲突"].append(_text(event.get("事件标识")))
            group["数据去向"].append({
                "事件标识": _text(event.get("事件标识")),
                "旧键": key,
                "新组合键": new_key,
                "旧分区": event.get("旧分区"),
                "目标分区": event.get("目标分区"),
                "归属状态": status,
                "有效归属": bool(event.get("有效归属")),
            })

        groups = list(grouped.values())
        for group in groups:
            changed = {
                item["目标分区"] for item in group["数据去向"]
                if item["有效归属"] and item["目标分区"] is not None
            }
            group["新键归属分区"] = sorted(changed)
            group["旧键压缩"] = len(group["新组合键"]) > 1
            group["范围命中"] = (not scope) or group["旧键"] in scope
        return {
            "批次编号": _text(batch.get("批次编号")),
            "状态": _text(batch.get("状态")),
            "旧批次范围": scope,
            "分组": groups,
        }

    def get_differences(self, batch_id: int) -> list[dict[str, Any]]:
        """供分组分区消费的差额；必须是已核对且已移交的批次。"""
        batch = self._require_batch(batch_id)
        if not batch.get("核对时间"):
            raise DedupMigrationError("批次尚未核对，没有可消费的双键分区迁移差额")
        if batch.get("状态") != STATUS_DELIVERED and not batch.get("移交时间"):
            raise DedupMigrationError("去重批次尚未移交差额，分组分区不能提前消费")
        return [dict(item) for item in batch.get("迁移差额") or []]

    # ------------------------------------------------------------------ 内部
    def _require_batch(self, batch_id: int) -> dict[str, Any]:
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            raise DedupMigrationError(f"迁移批次 {batch_id} 不存在或已归档")
        return batch

    def _guard_mutable(self, batch: dict[str, Any]) -> None:
        if batch.get("状态") == STATUS_DELIVERED:
            raise DedupMigrationError(
                f"迁移批次 {batch.get('批次编号')} 已交付，仅保留只读回查，不能再次修改")

    def _invalidate(self, batch: dict[str, Any], *, event_count: int) -> None:
        for field in ("核对时间", "移交时间"):
            batch[field] = None
        batch["核对摘要"] = {}
        batch["迁移差额"] = []
        batch["接管结论"] = []
        batch["事件总数"] = event_count
        batch["状态"] = STATUS_PENDING
        batch["pending"] = True
        batch["abnormal"] = False
        batch["迁移结论"] = "事件已更新，需要重新核对"
        batch["更新时间"] = _now()

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in batch.items() if key != "迁移差额"}
        view["旧批次范围"] = list(batch.get("旧批次范围") or [])
        view["来源授权"] = list(batch.get("来源授权") or [])
        view["差额数"] = len(batch.get("迁移差额") or [])
        view["接管结论"] = [dict(item) for item in batch.get("接管结论") or []]
        return view


service = DedupMigrationService()
