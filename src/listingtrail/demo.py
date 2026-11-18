"""离线可复现场景：多中介站抓取策略 + 配置更新时批次未完成 + 重启恢复。

运行 ``make demo`` 可看到：

1. 来源 alpha 被限速，来源 beta 不受影响；
2. 批次在配置更新前被中断，检查点保留旧策略版本；
3. 重启后沿用批次号与已有挂牌身份，请求轨迹同时包含旧/新决策版本；
4. 一次暂时失败不会把已有房源改写成“下架”。
"""
from __future__ import annotations

import json

from . import fetch_policy, pipeline, store


def _listing(rid, src, status="active", **extra):
    record = {
        "id": rid,
        "src": src,
        "name": rid.upper(),
        "value": 1,
        "status": status,
        "ts": "2026-11-01T00:00:00Z",
    }
    record.update(extra)
    return record


def run_demo():
    con = store.connect(":memory:")
    clock = {"t": 1000.0}

    base = fetch_policy.load_policies(
        {
            "alpha": {"rate_limit": {"min_interval_seconds": 30.0,
                                     "max_wait_seconds": 60.0}},
            "beta": {"rate_limit": {"min_interval_seconds": 0.0,
                                    "max_wait_seconds": 60.0}},
        }
    )
    feed = [
        _listing("a-1", "alpha"),
        _listing("a-2", "alpha"),
        _listing("b-1", "beta"),
        _listing("b-2", "beta"),
    ]

    first = pipeline.run(
        con=con,
        records=feed,
        policies=base,
        batch_id="demo-batch",
        resume=False,
        stop_after=1,
        now=lambda: clock["t"],
    )

    updated = fetch_policy.load_policies(
        {
            "alpha": {"rate_limit": {"min_interval_seconds": 5.0,
                                     "max_wait_seconds": 60.0}},
            "beta": {"rate_limit": {"min_interval_seconds": 0.0,
                                    "max_wait_seconds": 60.0}},
        }
    )
    resumed = pipeline.run(
        con=con,
        records=feed,
        policies=updated,
        resume=True,
        now=lambda: clock["t"],
    )

    identity_before = store.get(con, "a-1")["listing_identity"]
    outage = pipeline.run(
        con=con,
        records=[_listing("b-1", "beta", fetch={"http_status": 503})],
        policies=updated,
        resume=False,
        now=lambda: clock["t"],
    )
    after_outage = store.get(con, "b-1")

    return {
        "interrupted": first,
        "resumed": resumed,
        "policy_versions": [v["version"] for v in store.policy_versions(con)],
        "trace": [
            {
                "position": t["position"],
                "key": t["key"],
                "source": t["source"],
                "policy_version": t["policy_version"],
                "outcome": t["outcome"],
                "request_kind": t["request_kind"],
                "rate_limit_wait": t["payload"].get("rate_limit_wait_seconds"),
                "resume_position": t["payload"].get("resume_position"),
            }
            for t in store.fetch_trace(con, "demo-batch")
        ],
        "events": [
            {"kind": e["kind"], "payload": e["payload"]}
            for e in store.events(con, "demo-batch")
        ],
        "identity": identity_before,
        "outage": {
            "degraded": outage["degraded"],
            "listing_status": after_outage["listing_status"],
            "credible": after_outage["listing_snapshot"]["credible"],
            "failure_category": after_outage["listing_snapshot"]["failure_category"],
            "delisting_fact": after_outage["listing_snapshot"]["delisting_fact"],
        },
    }


def main():
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
