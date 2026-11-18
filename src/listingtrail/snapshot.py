"""单一职责：把一次观测固化成可追溯的房源快照（snapshot）。

快照只描述“这次看到了什么”，并显式携带决策版本、失败类别与恢复位置；
它不负责判定下架（那是 ``statusline``），也不改身份。

关键约束：
* ``blocked`` / ``temporarily_unavailable`` 产生的是“降级快照”，
  ``ok=False``、``delisting_fact=False``，绝不能被当作下架证据。
* ``not_modified`` 复用上一次成功快照的内容摘要，避免误判为空。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .fetch_policy import DEGRADED_OUTCOMES


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def process(record: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    rec = dict(record)
    decision = rec.get("fetch_decision")
    decision = dict(decision) if isinstance(decision, Mapping) else {}

    outcome = rec.get("fetch_outcome") or decision.get("outcome") or "ok"
    failure_category = rec.get("failure_category") or decision.get("failure_category")
    policy_version = rec.get("policy_version") or decision.get("policy_version")
    position = rec.get("resume_position", decision.get("position"))
    batch_id = decision.get("batch_id") or getattr(context, "batch_id", None)
    identity = rec.get("listing_identity") or {}
    prior = getattr(context, "prior", None) or rec.get("prior") or {}

    degraded = outcome in DEGRADED_OUTCOMES
    rendered_items = rec.get("rendered_items") or []
    observed_at = rec.get("ts") or decision.get("observed_at")

    prior_snapshot = prior.get("listing_snapshot") if isinstance(prior, Mapping) else None
    content_digest = _digest(
        {
            "items": rendered_items,
            "status": rec.get("listing_status"),
            "name": rec.get("name"),
            "value": rec.get("value"),
        }
    )
    if outcome == "not_modified" and isinstance(prior_snapshot, Mapping):
        content_digest = prior_snapshot.get("content_digest", content_digest)

    snapshot = {
        "batch_id": batch_id,
        "position": position,
        "source": decision.get("source") or rec.get("src"),
        "policy_version": policy_version,
        "decision_version": policy_version,
        "failure_category": failure_category,
        "resume_position": position,
        "outcome": outcome,
        "http_status": decision.get("http_status"),
        "request_kind": rec.get("request_kind") or decision.get("request_kind"),
        "credible": not degraded,
        "ok": not degraded,
        "delisting_fact": (
            not degraded
            and outcome not in {"not_modified", "empty_result"}
            and rec.get("listing_status") == "delisted"
        ),
        "status": rec.get("listing_status"),
        "identity_key": identity.get("key") or rec.get("identity_key"),
        "content_digest": content_digest,
        "observed_at": observed_at,
        "item_count": len(rendered_items),
    }

    rec["listing_snapshot"] = snapshot
    rec["snapshot_trace"] = {
        "policy_version": policy_version,
        "failure_category": failure_category,
        "resume_position": position,
        "outcome": outcome,
        "credible": snapshot["credible"],
    }
    rec["_stage_snapshot"] = "degraded" if degraded else "ok"
    return rec


def is_degraded(record: Mapping[str, Any]) -> bool:
    snapshot = record.get("listing_snapshot")
    if isinstance(snapshot, Mapping):
        return not snapshot.get("credible", True)
    return (record.get("fetch_outcome") in DEGRADED_OUTCOMES)
