"""单一职责：把来源回执渲染成结构化房源条目（renderer）。

渲染决策必须与 ``fetch_policy`` 的决策一致，因此这里只消费决策字段，
不自行猜测请求方式或失败类别：

* ``request_kind == "render"`` 时才做渲染式抽取；普通请求不升级。
* 决策为 ``blocked`` / ``temporarily_unavailable`` 时渲染空结果，
  并保留 ``failure_category`` / ``policy_version`` / ``resume_position``，
  让下游明确知道“没拿到内容”而不是“拿到零条”。
* ``not_modified`` 沿用上下文里的旧条目，不重新渲染。
* 渲染轨迹记录“为什么用这种渲染方式”，便于追踪普通→渲染的切换。
"""
from __future__ import annotations

from typing import Any, Mapping

from .fetch_policy import (
    OUTCOME_NOT_MODIFIED,
    OUTCOME_OK,
    OUTCOME_DELISTED,
)


def _raw_items(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "listings", "results", "properties"):
        value = record.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _render_item(item: Mapping[str, Any]) -> dict[str, Any]:
    rendered = dict(item)
    rendered.setdefault(
        "title", str(item.get("name") or item.get("title") or "").strip()
    )
    rendered.setdefault(
        "url", str(item.get("url") or item.get("detail_url") or "").strip()
    )
    return rendered


def process(record: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    rec = dict(record)
    decision = rec.get("fetch_decision")
    decision = dict(decision) if isinstance(decision, Mapping) else {}

    request_kind = rec.get("request_kind") or decision.get("request_kind") or "plain"
    outcome = rec.get("fetch_outcome") or decision.get("outcome") or OUTCOME_OK
    failure_category = rec.get("failure_category") or decision.get("failure_category")
    policy_version = rec.get("policy_version") or decision.get("policy_version")
    position = rec.get("resume_position", decision.get("position"))
    reason = decision.get("request_reason")
    escalation = decision.get("escalation")

    items: list[dict[str, Any]] = []
    rendered_from: str

    if outcome in (OUTCOME_OK, OUTCOME_DELISTED):
        items = [_render_item(item) for item in _raw_items(rec)]
        rendered_from = request_kind
    elif outcome == OUTCOME_NOT_MODIFIED:
        prior = getattr(context, "prior", None) or rec.get("prior") or {}
        prior_items = prior.get("rendered_items") if isinstance(prior, Mapping) else None
        items = [dict(item) for item in (prior_items or []) if isinstance(item, Mapping)]
        rendered_from = "prior_snapshot"
    else:
        # blocked / temporarily_unavailable / empty_result：不产出条目。
        items = []
        rendered_from = "none"

    trace = {
        "request_kind": request_kind,
        "request_reason": reason,
        "rendered_from": rendered_from,
        "outcome": outcome,
        "failure_category": failure_category,
        "policy_version": policy_version,
        "resume_position": position,
        "escalation": escalation,
    }

    rec["rendered_items"] = items
    rec["render_trace"] = trace
    rec["_stage_renderer"] = request_kind
    return rec
