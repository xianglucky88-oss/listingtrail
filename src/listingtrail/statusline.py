"""单一职责：挂牌状态流转（在售·下定·已售·下架）。

关键不变量：**只有来源确认的观测才能产生状态事实**。
``blocked`` / ``temporarily_unavailable`` 属于“本次没看到可信内容”，
不得改写已有状态；其中尤其是不能把暂时失败当成“下架”。

状态优先级：sold > delisted > reserved > active（未知观测只留痕，不推进）。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

STATUS_ACTIVE = "active"
STATUS_RESERVED = "reserved"
STATUS_SOLD = "sold"
STATUS_DELISTED = "delisted"
STATUS_UNKNOWN = "unknown"

TERMINAL = frozenset({STATUS_SOLD, STATUS_DELISTED})

_ALIASES = {
    STATUS_ACTIVE: {"active", "on_sale", "onsale", "for_sale", "available", "listing", "在售"},
    STATUS_RESERVED: {
        "reserved",
        "under_offer",
        "underoffer",
        "pending",
        "in_contract",
        "deposit",
        "下定",
    },
    STATUS_SOLD: {"sold", "closed", "completed", "sold_out", "已售", "成交"},
    STATUS_DELISTED: {
        "delisted",
        "removed",
        "withdrawn",
        "off_market",
        "offmarket",
        "expired",
        "下架",
    },
}

_RANK = {
    STATUS_UNKNOWN: 0,
    STATUS_ACTIVE: 1,
    STATUS_RESERVED: 2,
    STATUS_SOLD: 3,
    STATUS_DELISTED: 3,
}


def normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    for canonical, aliases in _ALIASES.items():
        if text in aliases:
            return canonical
    if re.search(r"sold|成交|已售", text):
        return STATUS_SOLD
    if re.search(r"delist|withdraw|下架|remove", text):
        return STATUS_DELISTED
    if re.search(r"reserv|under[_ ]?offer|下定", text):
        return STATUS_RESERVED
    if re.search(r"active|在售|on[_ ]?sale", text):
        return STATUS_ACTIVE
    return None


def _advance_state(prior_state: str, observed: str) -> str:
    """由既有状态与本次观测推导新状态。

    * 未观测（unknown）不推进状态；
    * 已售/已下架是终态，之后的普通观测不能把它改回去；
    * 其余按“在售 → 下定 → 已售/下架”的方向推进。
    """
    if observed == STATUS_UNKNOWN:
        return prior_state
    if prior_state in TERMINAL and observed != prior_state:
        return prior_state
    if _RANK.get(observed, 0) >= _RANK.get(prior_state, 0):
        return observed
    return observed if prior_state == STATUS_UNKNOWN else prior_state


def _prior_state(prior: Mapping[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(prior, Mapping):
        return STATUS_UNKNOWN, []
    history = prior.get("status_history")
    history = list(history) if isinstance(history, list) else []
    state = prior.get("listing_status") or prior.get("status")
    normalized = normalize_status(state)
    if normalized:
        return normalized, history
    if history:
        last = normalize_status(history[-1].get("status"))
        if last:
            return last, history
    return STATUS_UNKNOWN, history


def process(record: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    rec = dict(record)
    decision = rec.get("fetch_decision") if isinstance(rec.get("fetch_decision"), Mapping) else {}
    outcome = rec.get("fetch_outcome") or decision.get("outcome") or "ok"
    category = rec.get("failure_category") or decision.get("failure_category")

    prior = getattr(context, "prior", None) or rec.get("prior")
    prior_state, history = _prior_state(prior)

    observed = normalize_status(rec.get("status"))
    if outcome == "delisted":
        # 来源已确认下架（如 404/410/明确移除）优先于记录里的旧状态字段。
        observed = STATUS_DELISTED
    if observed is None:
        observed = STATUS_UNKNOWN

    degraded = outcome in {"blocked", "temporarily_unavailable"}
    history = list(history)

    if degraded:
        # 暂时失败/被拦截：状态保持不变，只追加一条“未观测”轨迹。
        state = prior_state if prior_state != STATUS_UNKNOWN else STATUS_UNKNOWN
        history.append(
            {
                "status": state,
                "observed": False,
                "outcome": outcome,
                "failure_category": category,
                "observed_at": rec.get("ts") or decision.get("observed_at"),
                "note": "no_credible_observation",
            }
        )
    elif outcome == "not_modified":
        state = prior_state if prior_state != STATUS_UNKNOWN else STATUS_UNKNOWN
        history.append(
            {
                "status": state,
                "observed": True,
                "outcome": outcome,
                "observed_at": rec.get("ts") or decision.get("observed_at"),
                "note": "not_modified",
            }
        )
    elif outcome == "empty_result":
        # 合法空结果：没看到这条挂牌 ≠ 它下架了；保持既有状态。
        state = prior_state if prior_state != STATUS_UNKNOWN else STATUS_UNKNOWN
        history.append(
            {
                "status": state,
                "observed": True,
                "outcome": outcome,
                "observed_at": rec.get("ts") or decision.get("observed_at"),
                "note": "absent_from_result_page",
            }
        )
    else:
        state = _advance_state(prior_state, observed)
        history.append(
            {
                "status": state,
                "observed": True,
                "outcome": outcome,
                "observed_at": rec.get("ts") or decision.get("observed_at"),
            }
        )

    transition = None
    if prior_state != STATUS_UNKNOWN and state != prior_state:
        transition = {"from": prior_state, "to": state, "outcome": outcome}

    rec["listing_status"] = state
    rec["status_history"] = history[-20:]
    rec["status_transition"] = transition
    rec["status_observed"] = outcome not in {"blocked", "temporarily_unavailable"}
    rec["_stage_statusline"] = state
    return rec


def is_delisting_fact(record: Mapping[str, Any]) -> bool:
    """该记录是否**新确立**了下架事实。

    必须同时满足：本次观测可信（非降级）、不是“未修改/空结果”、且状态为下架。
    暂时失败、拦截、304 或空结果都只是保留既有状态，不是新的下架证据。
    """
    decision = record.get("fetch_decision") or {}
    outcome = record.get("fetch_outcome") or decision.get("outcome") or "ok"
    if outcome in {"blocked", "temporarily_unavailable", "not_modified", "empty_result"}:
        return False
    snapshot = record.get("listing_snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("credible") is False:
        return False
    return record.get("listing_status") == STATUS_DELISTED
