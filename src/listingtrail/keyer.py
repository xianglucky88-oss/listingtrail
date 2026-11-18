"""单一职责：派生稳定主键（挂牌身份）。

身份语义保持不变：
* 有显式挂牌编号（``id``，兼容 ``external_id``/``listing_id``）时，主键仍是
  编号本身；这是既有语义，跨批次、跨策略版本、重启都不会改变。
* 已由 ``identity`` 阶段确定身份时，沿用其主键，绝不重算。
* 其余情况保持基线行为：对记录做稳定摘要。

``identity`` 阶段用 :func:`derived_key` 派生新挂牌的主键：它先剔除采集运行期
字段（策略版本、失败类别、快照、观测时间等）以及会随流转变化的状态/价格字段，
因此同一挂牌不会因策略变化或状态流转而换键；不含这些字段的记录，结果与
``key_of`` 的基线摘要一致。
"""
import hashlib
import json


_ID_FIELDS = ("id", "external_id", "listing_id", "source_listing_id")
_URL_FIELDS = ("url", "detail_url", "listing_url", "permalink")

#: 采集/策略运行期字段：不属于挂牌身份。
_VOLATILE_KEYS = {
    "_stage_fetch_policy",
    "_stage_renderer",
    "_stage_statusline",
    "_stage_snapshot",
    "batch_id",
    "policy_version",
    "decision_version",
    "fetch_decision",
    "fetch_outcome",
    "failure_category",
    "request_kind",
    "resume_position",
    "render_trace",
    "snapshot_trace",
    "listing_snapshot",
    "last_good_snapshot",
    "status_history",
    "status_transition",
    "status_observed",
    "listing_status",
    "listing_identity",
    "identity_key",
    "rendered_items",
    "observed_at",
    "ts",
    # 观测回执与渲染产物：属于采集过程，不是挂牌身份。
    "fetch",
    "items",
    "results",
    "listings",
    "properties",
    # 挂牌状态与价格会随流转变化，是历史事实而非身份依据。
    "status",
    "price",
    "value",
}


def _digest(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _raw_id(record):
    for name in _ID_FIELDS:
        value = record.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _url(record) -> str:
    for name in _URL_FIELDS:
        value = str(record.get(name, "")).strip()
        if value:
            return value
    return ""


def _identity_fields(record) -> dict:
    """用于摘要的字段：只剔除运行期字段，其余保持与基线一致（含嵌套值）。"""
    return {
        k: v
        for k, v in record.items()
        if k not in _VOLATILE_KEYS and not k.startswith("_")
    }


def identity_basis(record):
    """返回房源的身份依据（用于追踪，也是无 URL 新房源的派生依据）。"""
    src = str(record.get("src", "")).strip().lower()
    rid = _raw_id(record)
    if rid:
        return {"src": src, "id": rid}
    url = _url(record)
    if url:
        return {"src": src, "url": url}
    return {"src": src, "fields": _identity_fields(record)}


def key_of(record):
    """基线的稳定主键：显式编号优先，其次身份阶段结论，最后记录摘要。"""
    rid = _raw_id(record)
    if rid:
        return rid
    identity = record.get("listing_identity")
    if isinstance(identity, dict) and identity.get("key"):
        return str(identity["key"])
    return _digest(record)


def is_explicit(record):
    """该记录是否带有稳定的来源内挂牌编号。"""
    return bool(_raw_id(record))


def url_key(record):
    """无显式编号时的“来源 + 详情页 URL”查找键；无 URL 返回 None。"""
    url = _url(record)
    if not url:
        return None
    src = str(record.get("src", "")).strip().lower()
    return _digest({"src": src, "url": url})


def derived_key(record):
    """新挂牌的主键：显式编号 > 来源+URL > 剔除运行期字段后的记录摘要。

    最后一种与基线 ``key_of`` 的摘要口径一致；差别只在于先剔除策略版本、
    失败类别、快照等采集运行期字段，使身份不随策略变化而漂移。
    """
    rid = _raw_id(record)
    if rid:
        return rid
    url = url_key(record)
    if url:
        return url
    return _digest(_identity_fields(record))


def candidates(record):
    """返回可用于查找既有记录的主键候选（按可信度排序）。"""
    rid = _raw_id(record)
    if rid:
        return [rid]
    out = []
    for candidate in (url_key(record), derived_key(record), key_of(record)):
        if candidate and candidate not in out:
            out.append(candidate)
    return out
