"""去重换键的纯函数：组合键拼装、事件指纹、键口径判定与分区落点。

这些函数不碰 store，seed 生成示例迁移和业务服务共用同一套口径，
保证「核对、差额、接管、回写」算出来的分区号一致。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 新组合键的字段分隔符；登记时允许用 |、/、+、, 中任意一种书写。
KEY_SEPARATORS = "|/+,"
KEY_SCOPES = ("旧键", "双键", "新键")

# 这些字段是迁移过程产生的元数据，不参与事件指纹，否则重复事件无法识别。
VOLATILE_FIELDS = {
    "id", "迁移编号", "事件标识", "键口径", "事件指纹",
    "旧键", "旧分区", "目标分区", "归属状态", "有效归属",
    "冲突原因", "归属事件标识", "是否差额", "来源授权",
}


def split_key_spec(spec: str) -> list[str]:
    """把「订单编号|来源编号|业务身份」拆成字段名列表，去掉空段。"""
    text = str(spec or "").strip()
    if not text:
        return []
    normalized = text
    for separator in KEY_SEPARATORS:
        normalized = normalized.replace(separator, "|")
    return [part.strip() for part in normalized.split("|") if part.strip()]


def compose_new_key(event: dict[str, Any], spec: str) -> str:
    """按组合键定义从一条事件里取值拼装新键，缺失字段按空串处理。"""
    if str(event.get("新组合键") or "").strip():
        return str(event["新组合键"]).strip()
    parts = split_key_spec(spec)
    if not parts:
        return ""
    values = [str(event.get(part, "") if event.get(part) is not None else "").strip() for part in parts]
    return "|".join(values)


def event_fingerprint(event: dict[str, Any]) -> str:
    """事件指纹：同一份业务负载得到同一个指纹，用于识别完全重复。

    事件登记时显式给出的指纹优先；否则对除迁移元数据外的全部字段做
    规范化 JSON 摘要。事件标识本身不参与摘要，所以重复投递的副本会撞指纹。
    """
    explicit = str(event.get("事件指纹") or "").strip()
    if explicit:
        return explicit
    body = {key: value for key, value in event.items() if key not in VOLATILE_FIELDS}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def infer_key_scope(event: dict[str, Any], new_key_spec: str, old_key_field: str) -> str:
    """判定单条事件采用的键口径：旧键、双键还是新键。

    显式声明优先；未声明时按事件是否同时携带旧订单键和新组合键字段推断。
    边界批里的每一条事件都会把这个口径落到明细上，便于逐条回查。
    """
    declared = str(event.get("键口径") or "").strip()
    if declared in KEY_SCOPES:
        return declared
    has_old = bool(str(event.get(old_key_field, "") if event.get(old_key_field) is not None else "").strip())
    parts = split_key_spec(new_key_spec)
    has_new = bool(parts) and all(
        str(event.get(part, "") if event.get(part) is not None else "").strip() for part in parts)
    if has_old and has_new:
        return "双键"
    if has_new:
        return "新键"
    return "旧键"


def partition_of(key: str, partition_count: int, algorithm: str = "") -> int:
    """把键落到分区号：默认对 SHA-256 摘要取模。

    当前示例策略同时支持哈希取模与一致性哈希，两者在本平台里都采用
    稳定的摘要取模实现，避免同一份数据在不同调用方算出不同分区。
    """
    try:
        count = int(partition_count)
    except (TypeError, ValueError):
        count = 10
    if count <= 0:
        count = 10
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    if algorithm in ("范围分区", "range"):
        # 范围分区：把摘要映射到 [0, count) 的连续区间。
        return (int(digest[:8], 16) * count) // (1 << 32)
    return int(digest, 16) % count


def normalize_sources(value: Any) -> list[str]:
    """授权来源允许数组，也允许用逗号、顿号、分号分隔的字符串。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    for separator in ("，", "、", ";", "；", "\n"):
        text = text.replace(separator, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def normalize_old_keys(value: Any) -> list[str]:
    """批次覆盖的旧键列表同样兼容数组和分隔字符串。"""
    return normalize_sources(value)
