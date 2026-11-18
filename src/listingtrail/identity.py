"""单一职责：确定挂牌的稳定身份。

身份语义是“已有挂牌不得因采集策略、批次中断或重启而改变”：

* 来源内显式挂牌编号永远优先，且与基线 ``keyer.key_of`` 一致。
* 无编号但有详情页 URL 时，用“来源 + URL”派生主键，绝不把请求结果、策略
  版本、失败类别、时间戳混入身份依据。
* 无编号也无 URL 时，沿用基线摘要口径，但先剔除采集运行期字段
  （由 ``keyer.derived_key`` 负责）。
* 若上下文中已有同一挂牌的历史记录（``context.prior``），沿用其主键与
  ``stable_since``，只补充观测到的新别名。
"""
from __future__ import annotations

from typing import Any, Mapping

from .keyer import _raw_id, candidates, derived_key, identity_basis


def process(record: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    rec = dict(record)
    source = str(rec.get("src") or rec.get("source") or "").strip().lower()
    prior = getattr(context, "prior", None) or rec.get("prior")

    explicit = bool(_raw_id(rec))
    basis = identity_basis(rec)
    key = derived_key(rec)

    stable_since = None
    aliases: list[str] = []
    if isinstance(prior, Mapping):
        prior_identity = prior.get("listing_identity")
        if isinstance(prior_identity, Mapping):
            # 历史主键就是身份本身：策略变化/重启都不能重算它。
            if prior_identity.get("key"):
                key = str(prior_identity["key"])
            stable_since = prior_identity.get("stable_since")
            aliases = list(prior_identity.get("aliases") or [])
        elif prior.get("key"):
            key = str(prior["key"])
        # 保留来源内编号的历史别名，便于跨站归并时追溯。
        prior_id = _raw_id(prior)
        if prior_id and prior_id not in aliases:
            aliases.append(prior_id)

    identity = {
        "key": key,
        "source": source,
        "explicit_id": explicit,
        "basis": basis,
        "stable_since": stable_since or rec.get("ts") or rec.get("observed_at"),
        "aliases": aliases,
        "lookup_keys": candidates(rec),
    }
    rec["listing_identity"] = identity
    rec["identity_key"] = key
    rec["_stage_identity"] = "stable"
    return rec
