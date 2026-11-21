"""去重换键迁移与分组分区接管的验证：三组边界 + 中断对账 + 回写规则。"""
import pytest

from app.services.dedup_migration import (
    DedupMigrationError,
    service as dedup,
)
from app.services.grp_partition_migration import (
    GrpPartitionMigrationError,
    handover_service,
)
from app.store import store


def make_batch(code="MIG-T1", *, sources=("SOUR-A",), scope=("ORD-9001",),
               confirmed=False, partitions=10, spec="订单编号|来源编号|业务身份"):
    batch, errors = dedup.create_batch({
        "批次编号": code,
        "关联策略": "DEDU-0002",
        "旧键字段": "订单编号",
        "新组合键": spec,
        "来源授权": list(sources),
        "旧批次范围": list(scope),
        "旧批次已确认": confirmed,
        "分区数量": partitions,
    })
    assert errors == [], errors
    return batch


def event(event_id, order, source, identity, amount=1.0, **extra):
    payload = {
        "事件标识": event_id,
        "订单编号": order,
        "来源编号": source,
        "业务身份": identity,
        "金额": amount,
    }
    payload.update(extra)
    return payload


def reconcile(batch_id):
    return dedup.reconcile(int(batch_id))


def events_of(code):
    return {e["事件标识"]: e for e in reconcile(code)["events"]}


class TestReconcileThreeGroups:
    def test_same_old_key_different_source_rejected(self):
        """同旧键不同来源：只有授权来源能改分区，其余退回源端。"""
        batch = make_batch(sources=("SOUR-A",), scope=("ORD-9101",))
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9101", "SOUR-A", "BIZ-31"),
            event("E-2", "ORD-9101", "SOUR-B", "BIZ-31"),
        ])
        detail = reconcile(batch["id"])
        events = {e["事件标识"]: e for e in detail["events"]}
        assert events["E-1"]["归属状态"] == "通过"
        assert events["E-1"]["有效归属"] is True
        assert events["E-2"]["归属状态"] == "拒绝"
        assert events["E-2"]["有效归属"] is False
        assert "跨来源修改分区" in events["E-2"]["冲突原因"]
        # 拒绝的记录仍然作为差额交给分组分区，由下游退回源端。
        assert {d["事件标识"] for d in detail["迁移差额"]} == {"E-1", "E-2"}

    def test_same_key_same_source_different_identity_conflict(self):
        """同旧键同来源但业务身份不同：旧批次未确认时整组有效归属冲突。"""
        batch = make_batch(scope=("ORD-9001",))
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9001", "SOUR-A", "BIZ-31", 1.0),
            event("E-2", "ORD-9001", "SOUR-A", "BIZ-32", 2.0),
        ])
        detail = reconcile(batch["id"])
        assert detail["状态"] == "归属冲突"
        assert detail["abnormal"] is True
        assert detail["摘要"]["冲突数"] == 2
        assert detail["摘要"]["有效归属数"] == 0
        assert all(e["归属状态"] == "冲突" for e in detail["events"])
        assert all("旧批次尚未确认" in e["冲突原因"] for e in detail["events"])

    def test_identical_replica_counts_once(self):
        """完全重复：同事件标识或同内容投递都只保留一个有效归属。"""
        batch = make_batch(scope=("ORD-9201",))
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9201", "SOUR-A", "BIZ-40", 7.0),
            event("E-1", "ORD-9201", "SOUR-A", "BIZ-40", 7.0),
            event("E-2", "ORD-9201", "SOUR-A", "BIZ-40", 7.0),
        ])
        detail = reconcile(batch["id"])
        events = detail["events"]
        assert detail["摘要"]["通过数"] == 1
        assert detail["摘要"]["重复数"] == 2
        assert detail["摘要"]["有效归属数"] == 1
        assert events[0]["有效归属"] is True
        for replica in events[1:]:
            assert replica["归属状态"] == "重复"
            assert replica["有效归属"] is False

    def test_same_event_id_different_payload_conflicts(self):
        batch = make_batch(scope=("ORD-9301",))
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9301", "SOUR-A", "BIZ-41", 1.0),
            event("E-1", "ORD-9301", "SOUR-A", "BIZ-41", 99.0),
        ])
        replicas = [e for e in reconcile(batch["id"])["events"] if e["事件标识"] == "E-1"]
        # 同标识不同负载：第一笔通过，第二笔记为冲突；整组只会有一个有效归属。
        assert sum(1 for e in replicas if e["有效归属"]) == 1
        statuses = {e["归属状态"] for e in replicas}
        assert "冲突" in statuses and "通过" in statuses


class TestBatchScopeAndKeyScope:
    def test_out_of_scope_old_key_is_explainable(self):
        batch = make_batch(scope=("ORD-9401",))
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9401", "SOUR-A", "BIZ-50"),
            event("E-2", "ORD-9501", "SOUR-A", "BIZ-51"),
        ])
        events = events_of(batch["id"])
        assert events["E-1"]["归属状态"] == "通过"
        assert events["E-2"]["归属状态"] == "超出范围"
        assert "ORD-9501" in events["E-2"]["冲突原因"]

    def test_key_scope_recorded_per_event(self):
        batch = make_batch(scope=("ORD-9601",))
        dedup.ingest_events(batch["id"], [
            event("E-old", "ORD-9601", "SOUR-A", ""),
            event("E-pair", "ORD-9601", "SOUR-A", "BIZ-60"),
        ])
        events = events_of(batch["id"])
        assert events["E-old"]["键口径"] == "旧键"
        assert events["E-old"]["归属状态"] == "仅旧键"
        assert events["E-pair"]["键口径"] == "双键"
        assert events["E-pair"]["归属状态"] == "通过"

    def test_difference_only_contains_moved_partitions(self):
        batch = make_batch(scope=("ORD-9701",))
        # 这条事件的新键哈希落回旧分区，不属于迁移差额。
        dedup.ingest_events(batch["id"], [
            event("E-1", "ORD-9701", "SOUR-A", "BIZ-70"),
        ])
        detail = reconcile(batch["id"])
        for item in detail["迁移差额"]:
            assert item["旧分区"] != item["目标分区"]
        assert all(item["键口径"] == "双键" for item in detail["迁移差额"])


class TestHandoverWriteBack:
    def test_consume_requires_handover(self):
        batch = make_batch(code="MIG-GATE", scope=("ORD-9801",))
        dedup.ingest_events(batch["id"], [event("E-1", "ORD-9801", "SOUR-A", "BIZ-82")])
        dedup.reconcile(batch["id"])
        with pytest.raises(GrpPartitionMigrationError, match="尚未移交"):
            handover_service.consume(batch["id"], 1)
        dedup.run_action(batch["id"], "移交差额")
        result = handover_service.consume(batch["id"], 1)
        assert result["接管数"] == 1
        assert result["切换清单"][0]["事件标识"] == "E-1"

    def test_cross_source_handover_returned_to_source(self):
        batch = dedup.find_by_code("MIG-DEDUP-01")
        dedup.run_action(int(batch["id"]), "移交差额")
        result = handover_service.consume(int(batch["id"]), 2)
        decisions = {c["事件标识"]: c for c in result["切换清单"]}
        # 事件 EV-M1-002 来自未授权的 SOUR-B，即使分区越界也要按来源归属退回。
        assert decisions["EV-M1-002"]["接管结论"] == "退回源端"
        assert decisions["EV-M1-001"]["接管结论"] == "已接管"
        assert decisions["EV-M1-001"]["目标归属"] is True

    def test_switch_entries_keyed_by_event_identity(self):
        batch = dedup.find_by_code("MIG-DEDUP-01")
        dedup.run_action(int(batch["id"]), "移交差额")
        handover_service.consume(int(batch["id"]), 1)
        handover_service.consume(int(batch["id"]), 1)
        entries = handover_service.switch_entries("MIG-DEDUP-01")
        assert len(entries) == 2
        assert {e["事件标识"] for e in entries} == {"EV-M1-001", "EV-M1-002"}

    def test_conflict_waits_for_old_batch_confirmation(self):
        batch = dedup.find_by_code("MIG-DEDUP-02")
        batch_id = int(batch["id"])
        dedup.run_action(batch_id, "移交差额")
        first = handover_service.consume(batch_id, 2)
        assert first["接管数"] == 0
        assert first["待确认数"] == 2

        dedup.run_action(batch_id, "确认旧批次")
        second = handover_service.consume(batch_id, 2)
        assert second["接管数"] == 2
        assert second["待确认数"] == 0
        entries = {e["事件标识"]: e for e in handover_service.switch_entries("MIG-DEDUP-02")}
        assert all(e["接管结论"] == "已接管" for e in entries.values())
        assert dedup.get_batch(batch_id)["状态"] == "已移交"

    def test_disabled_group_cannot_take_over(self):
        batch = dedup.find_by_code("MIG-DEDUP-01")
        dedup.run_action(int(batch["id"]), "移交差额")
        store.find("grp_partition", 3)["status"] = "已停用"
        with pytest.raises(GrpPartitionMigrationError, match="已停用"):
            handover_service.consume(int(batch["id"]), 3)


class TestDeliveredReadOnly:
    def test_delivered_batch_rejects_mutation(self):
        batch_id = int(dedup.find_by_code("MIG-DEDUP-03")["id"])
        entry, message = dedup.run_action(batch_id, "开始核对")
        assert entry is None
        assert "只读" in message
        with pytest.raises(DedupMigrationError):
            dedup.ingest_events(batch_id, [event("E-X", "ORD-3001", "SOUR-A", "BIZ-99")])

    def test_legacy_view_explains_scope_by_old_key(self):
        batch_id = int(dedup.find_by_code("MIG-DEDUP-03")["id"])
        view = dedup.legacy_view(batch_id, old_key="ORD-3001")
        assert view["只读"] is True
        assert "ORD-3001" in view["范围说明"]
        assert {e["事件标识"] for e in view["事件"]} == {"EV-M3-001", "EV-M3-002"}
        assert all(e["旧键"] == "ORD-3001" for e in view["事件"])
        assert view["两代键对账"][0]["新组合键"]

    def test_empty_legacy_view_for_other_old_key(self):
        batch_id = int(dedup.find_by_code("MIG-DEDUP-03")["id"])
        view = dedup.legacy_view(batch_id, old_key="ORD-9999")
        assert view["事件"] == []

    def test_delivered_batch_consume_does_not_rewrite(self):
        batch_id = int(dedup.find_by_code("MIG-DEDUP-03")["id"])
        before = dict(store.find("_dedup_migrations", batch_id))
        result = handover_service.consume(batch_id, 1)
        assert result["接管数"] == 2
        after = store.find("_dedup_migrations", batch_id)
        assert after["迁移结论"] == before["迁移结论"]
        assert after["接管结论"] == before["接管结论"]


class TestInterruptedReconciliation:
    def test_interrupt_keeps_two_generation_ledger(self):
        batch = make_batch(code="MIG-INT", scope=("ORD-2001",))
        batch_id = int(batch["id"])
        dedup.ingest_events(batch_id, [
            event("E-1", "ORD-2001", "SOUR-A", "BIZ-02"),
            event("E-2", "ORD-2001", "SOUR-B", "BIZ-02"),
            event("E-3", "ORD-2001", "SOUR-A", "BIZ-03"),
        ])
        dedup.reconcile(batch_id)
        entry, _ = dedup.run_action(batch_id, "中断迁移")
        assert entry["状态"] == "已中断"

        view = dedup.reconciliation_view(batch_id)
        group = {g["旧键"]: g for g in view["分组"]}["ORD-2001"]
        # 中断后仍能说明每笔事件落在旧键/新键下的去向。
        assert group["事件数"] == 3
        assert len(group["新组合键"]) == 3
        assert group["拒绝"] == ["E-2"]

        resumed, _ = dedup.run_action(batch_id, "继续迁移")
        assert resumed["状态"] in ("归属冲突", "待移交")
        assert len(resumed["迁移差额"]) >= 1

    def test_reconciliation_after_confirm_splits_compressed_key(self):
        batch_id = int(dedup.find_by_code("MIG-DEDUP-02")["id"])
        dedup.run_action(batch_id, "确认旧批次")
        view = dedup.reconciliation_view(batch_id, old_key="ORD-2001")
        group = view["分组"][0]
        assert group["旧键压缩"] is True
        assert group["有效归属数"] == 2
        assert len(group["新组合键"]) == 2
        assert group["新键归属分区"] == sorted(
            target["目标分区"] for target in group["新组合键"].values())
