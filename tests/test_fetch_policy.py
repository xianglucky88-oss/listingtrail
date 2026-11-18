"""离线测试：来源级采集策略、失败分类、恢复与身份语义。

覆盖题目要求的场景：
* 来源 A 限速、来源 B 正常；
* 配置更新时批次尚未完成；
* 重启后的请求轨迹与已有房源身份；
* 暂时失败不得变成下架事实；
* 熔断期间探测 / 普通请求转渲染的可追踪选择。
"""
import json

from src.listingtrail import fetch_policy, pipeline, store
from src.listingtrail.keyer import key_of


def seeded(tmp_path):
    return store.connect(str(tmp_path / "t.db"))


def listing(rid, src, status="active", **extra):
    record = {"id": rid, "src": src, "name": rid.upper(), "value": 1,
              "status": status, "ts": "2026-11-01T00:00:00Z"}
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# 来源级策略与失败分类
# ---------------------------------------------------------------------------


def test_policy_version_changes_when_config_changes():
    base = fetch_policy.load_policies()
    changed = fetch_policy.load_policies(
        {"alpha": {"rate_limit": {"min_interval_seconds": 5.0}}}
    )
    assert base.version != changed.version
    assert base.version == fetch_policy.load_policies().version
    assert base.for_source("ALPHA").source == "alpha"


def test_source_policies_are_per_source():
    policies = fetch_policy.load_policies(
        {
            "alpha": {"rate_limit": {"min_interval_seconds": 5.0}},
            "beta": {"rate_limit": {"min_interval_seconds": 0.0}},
        }
    )
    assert policies.for_source("alpha").rate_limit.min_interval_seconds == 5.0
    assert policies.for_source("beta").rate_limit.min_interval_seconds == 0.0


def test_blocked_is_not_empty_result():
    policies = fetch_policy.load_policies()
    blocked = fetch_policy.classify_observation(
        {"http_status": 403}, policies.for_source("alpha")
    )
    empty = fetch_policy.classify_observation(
        {"items": []}, policies.for_source("alpha")
    )
    assert blocked[0] == fetch_policy.OUTCOME_BLOCKED
    assert blocked[1] == fetch_policy.CATEGORY_ACCESS_DENIED
    assert empty[0] == fetch_policy.OUTCOME_EMPTY
    assert empty[1] is None


def test_delisted_requires_confirmation():
    policies = fetch_policy.load_policies()
    assert fetch_policy.classify_observation(
        {"http_status": 404}, policies.for_source("alpha")
    )[0] == fetch_policy.OUTCOME_DELISTED
    strict = fetch_policy.load_policies({"alpha": {"treat_404_as_delisted": False}})
    outcome = fetch_policy.classify_observation(
        {"http_status": 404}, strict.for_source("alpha")
    )
    assert outcome[0] == fetch_policy.OUTCOME_TEMPORARY


# ---------------------------------------------------------------------------
# 限速来源 A / 正常来源 B
# ---------------------------------------------------------------------------


def test_rate_limited_source_a_does_not_block_source_b():
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    policies = fetch_policy.load_policies(
        {
            "alpha": {"rate_limit": {"min_interval_seconds": 10.0,
                                     "max_wait_seconds": 30.0}},
            "beta": {"rate_limit": {"min_interval_seconds": 0.0,
                                    "max_wait_seconds": 30.0}},
        }
    )
    session = fetch_policy.Session(policies=policies, now=now)

    first_a = session.observe(listing("a-1", "alpha"), position=0, batch_id="b")
    clock["t"] += 1
    second_a = session.observe(listing("a-2", "alpha"), position=1, batch_id="b")
    first_b = session.observe(listing("b-1", "beta"), position=2, batch_id="b")

    assert first_a.rate_limited is False
    assert second_a.rate_limited is True
    assert second_a.rate_limit_wait_seconds > 0
    assert second_a.source == "alpha"
    # 来源 B 不受 A 的限速影响。
    assert first_b.rate_limited is False
    assert first_b.outcome == fetch_policy.OUTCOME_OK
    trace_sources = {item["source"] for item in session.trace}
    assert trace_sources == {"alpha", "beta"}


def test_rate_limit_budget_exhausted_is_temporary_not_delisted():
    clock = {"t": 0.0}
    policies = fetch_policy.load_policies(
        {"alpha": {"rate_limit": {"min_interval_seconds": 100.0,
                                  "max_wait_seconds": 5.0}}}
    )
    session = fetch_policy.Session(policies=policies, now=lambda: clock["t"])
    session.observe(listing("a-1", "alpha"), position=0)
    clock["t"] += 1
    deferred = session.observe(listing("a-2", "alpha"), position=1)
    assert deferred.request_kind == "none"
    assert deferred.outcome == fetch_policy.OUTCOME_TEMPORARY
    assert deferred.failure_category == fetch_policy.CATEGORY_RATE_LIMITED
    assert deferred.request_reason == "rate_limit_wait_exceeds_budget"
    # 预算耗尽只是“本次没请求”，绝不能被当成下架。
    assert deferred.outcome != fetch_policy.OUTCOME_DELISTED


# ---------------------------------------------------------------------------
# 暂时失败不得成为下架事实
# ---------------------------------------------------------------------------


def test_temporary_failure_keeps_listing_identity_and_state(tmp_path):
    con = seeded(tmp_path)
    try:
        profile = [listing("a-1", "alpha",
                           items=[{"id": "a-1", "name": "A", "url": "u"}])]
        pipeline.run(con=con, records=profile, resume=False, batch_id="b1")

        before = store.get(con, "a-1")
        assert before["listing_status"] == "active"
        assert before["listing_identity"]["key"] == "a-1"

        outage = [listing("a-1", "alpha", fetch={"http_status": 503})]
        result = pipeline.run(con=con, records=outage, resume=False, batch_id="b2")
        after = store.get(con, "a-1")

        assert result["degraded"] == 1
        assert after["listing_identity"]["key"] == "a-1"
        assert after["listing_identity"]["stable_since"] == \
            before["listing_identity"]["stable_since"]
        assert after["listing_status"] == "active"
        assert after["listing_snapshot"]["credible"] is False
        assert after["listing_snapshot"]["delisting_fact"] is False
        assert after["listing_snapshot"]["failure_category"] == "server_error"
        assert after["fetch_outcome"] == fetch_policy.OUTCOME_TEMPORARY
        # 降级观测不丢掉上次成功渲染的房源条目。
        assert after["rendered_items"] == before["rendered_items"]
        assert after["last_good_snapshot"]["credible"] is True
    finally:
        con.close()


def test_empty_result_is_not_delisting(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, records=[listing("a-1", "alpha")],
                     resume=False, batch_id="b1")
        pipeline.run(con=con,
                     records=[listing("a-1", "alpha", fetch={"items": []})],
                     resume=False, batch_id="b2")
        stored = store.get(con, "a-1")
        assert stored["fetch_outcome"] == fetch_policy.OUTCOME_EMPTY
        assert stored["listing_status"] == "active"
        assert stored["listing_snapshot"]["delisting_fact"] is False
    finally:
        con.close()


def test_confirmed_404_becomes_delisted(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, records=[listing("a-1", "alpha")],
                     resume=False, batch_id="b1")
        pipeline.run(con=con,
                     records=[listing("a-1", "alpha", fetch={"http_status": 404})],
                     resume=False, batch_id="b2")
        stored = store.get(con, "a-1")
        assert stored["listing_status"] == "delisted"
        assert stored["listing_snapshot"]["delisting_fact"] is True
        assert stored["status_transition"] == {
            "from": "active", "to": "delisted", "outcome": "delisted"
        }
        # 之后来源又报暂时不可用：已确认的下架事实不能被撤回。
        pipeline.run(con=con, resume=False, batch_id="b3",
                     records=[listing("a-1", "alpha", fetch={"http_status": 503})])
        again = store.get(con, "a-1")
        assert again["listing_status"] == "delisted"
        assert again["listing_snapshot"]["credible"] is False
        assert again["last_good_snapshot"]["delisting_fact"] is True
    finally:
        con.close()


def test_blocked_page_with_zero_items_stays_active(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, records=[listing("a-1", "alpha")],
                     resume=False, batch_id="b1")
        pipeline.run(con=con, resume=False, batch_id="b2", records=[
            listing("a-1", "alpha", fetch={"http_status": 403, "items": []})
        ])
        stored = store.get(con, "a-1")
        assert stored["listing_status"] == "active"
        assert stored["fetch_outcome"] == fetch_policy.OUTCOME_BLOCKED
        assert stored["listing_snapshot"]["credible"] is False
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 熔断与探测
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_and_probe_is_traceable():
    clock = {"t": 500.0}
    policies = fetch_policy.load_policies(
        {
            "alpha": {
                "circuit_breaker": {
                    "failure_threshold": 2,
                    "cooldown_seconds": 20.0,
                    "allow_probe": True,
                    "probe_after_seconds": 20.0,
                }
            }
        }
    )
    session = fetch_policy.Session(policies=policies, now=lambda: clock["t"])
    for i in range(3):
        session.observe(listing(f"a-{i}", "alpha", fetch={"http_status": 503}),
                        position=i)

    third = session.trace[-1]
    assert third["breaker_state"] == fetch_policy.BREAKER_OPEN

    blocked_step = session.observe(listing("a-9", "alpha"), position=3)
    assert blocked_step.request_kind == "none"
    assert blocked_step.failure_category == fetch_policy.CATEGORY_CIRCUIT_OPEN

    clock["t"] += 21
    probe = session.observe(listing("a-10", "alpha"), position=4)
    assert probe.breaker_probe is True
    assert probe.request_kind == "plain"
    assert probe.outcome == fetch_policy.OUTCOME_OK
    assert probe.breaker_state == fetch_policy.BREAKER_CLOSED


def test_circuit_breaker_probe_can_be_disabled():
    clock = {"t": 0.0}
    policies = fetch_policy.load_policies(
        {"alpha": {"circuit_breaker": {"failure_threshold": 1,
                                       "cooldown_seconds": 1.0,
                                       "allow_probe": False}}}
    )
    session = fetch_policy.Session(policies=policies, now=lambda: clock["t"])
    session.observe(listing("a-1", "alpha", fetch={"http_status": 503}), position=0)
    clock["t"] += 3600
    decision = session.observe(listing("a-2", "alpha"), position=1)
    assert decision.request_kind == "none"
    assert decision.outcome == fetch_policy.OUTCOME_TEMPORARY


# ---------------------------------------------------------------------------
# 普通请求 → 渲染的切换
# ---------------------------------------------------------------------------


def test_blocked_plain_request_escalates_to_render():
    policies = fetch_policy.load_policies(
        {"alpha": {"request_mode": "plain", "render_on_block": True}}
    )
    session = fetch_policy.Session(policies=policies, now=lambda: 0.0)
    decision = session.observe(
        listing("a-1", "alpha", fetch={"http_status": 403}), position=0
    )
    assert decision.request_kind == "plain"
    assert decision.escalation == {
        "requested": True,
        "from": "plain",
        "to": "render",
        "reason": "blocked_plain",
        "next_request": "render",
        "hold_records": None,
    }
    followed = session.observe(listing("a-2", "alpha"), position=1)
    assert followed.request_kind == "render"
    assert followed.request_reason == "escalated_after_block"


def test_policy_can_render_first():
    policies = fetch_policy.load_policies({"alpha": {"request_mode": "render"}})
    session = fetch_policy.Session(policies=policies, now=lambda: 0.0)
    decision = session.observe(listing("a-1", "alpha"), position=0)
    assert decision.request_kind == "render"
    assert decision.request_reason == "policy_render_first"
    assert decision.conditional is False


def test_render_failure_does_not_emit_items(tmp_path):
    con = seeded(tmp_path)
    try:
        policies = fetch_policy.load_policies({"alpha": {"request_mode": "render"}})
        pipeline.run(con=con, policies=policies, resume=False, batch_id="b1",
                     records=[listing("a-1", "alpha",
                                      fetch={"http_status": 503, "items": [{"id": "x"}]})])
        stored = store.get(con, "a-1")
        assert stored["rendered_items"] == []
        assert stored["render_trace"]["rendered_from"] == "none"
        assert stored["render_trace"]["policy_version"] == policies.version
    finally:
        con.close()


def test_conditional_request_headers_carry_over():
    policies = fetch_policy.load_policies()
    session = fetch_policy.Session(policies=policies, now=lambda: 0.0)
    session.observe(listing("a-1", "alpha", fetch={"etag": 'W/"v1"'}))
    second = session.observe(listing("a-1", "alpha", fetch={"http_status": 304}))
    first = session.trace[0]
    assert first["conditional"] is False
    assert second.request_headers == {"If-None-Match": 'W/"v1"'}
    assert second.conditional is True
    assert second.outcome == fetch_policy.OUTCOME_NOT_MODIFIED


def test_not_modified_keeps_previous_rendered_items(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, resume=False, batch_id="b1", records=[
            listing("a-1", "alpha", fetch={"etag": 'W/"v1"'},
                    items=[{"id": "a-1", "title": "A"}])
        ])
        pipeline.run(con=con, resume=False, batch_id="b2", records=[
            listing("a-1", "alpha", fetch={"http_status": 304})
        ])
        stored = store.get(con, "a-1")
        assert stored["fetch_outcome"] == fetch_policy.OUTCOME_NOT_MODIFIED
        assert stored["rendered_items"] == [{"id": "a-1", "title": "A", "url": ""}]
        assert stored["listing_status"] == "active"
        assert stored["listing_snapshot"]["credible"] is True
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 批次中断 / 配置更新 / 重启恢复
# ---------------------------------------------------------------------------


def test_resume_trace_identity_across_restart_and_policy_change(tmp_path):
    db = tmp_path / "r.db"
    clock = {"t": 100.0}
    base = fetch_policy.load_policies()
    feed = [
        listing("a-1", "alpha"),
        listing("a-2", "beta"),
        listing("a-3", "alpha"),
    ]

    con = store.connect(str(db))
    try:
        interrupted = pipeline.run(
            con=con, records=feed, policies=base, resume=False,
            batch_id="b-resume", stop_after=1, now=lambda: clock["t"],
        )
        assert interrupted["interrupted"] is True
        assert interrupted["resume_position"] == 0
        identity_before = store.get(con, "a-1")["listing_identity"]["key"]
    finally:
        con.close()

    # 重启：新连接、策略配置已更新、批次未完成。
    updated = fetch_policy.load_policies(
        {"alpha": {"rate_limit": {"min_interval_seconds": 1.0}}}
    )
    assert updated.version != base.version
    con = store.connect(str(db))
    try:
        resumed = pipeline.run(
            con=con, records=feed, policies=updated, resume=True,
            now=lambda: clock["t"],
        )
        assert resumed["batch_id"] == "b-resume"
        assert resumed["resume_from"] == 1
        assert resumed["fed"] == 2
        assert resumed["rows"] == 3

        trace = store.fetch_trace(con, "b-resume")
        assert [t["key"] for t in trace] == ["a-1", "a-2", "a-3"]
        assert trace[0]["policy_version"] == base.version
        assert trace[1]["decision_version"] == updated.version
        assert trace[-1]["policy_version"] == updated.version

        kinds = [event["kind"] for event in store.events(con, "b-resume")]
        assert "batch_interrupted" in kinds
        assert "batch_resumed" in kinds
        assert "policy_changed_mid_batch" in kinds

        change = next(e for e in store.events(con, "b-resume")
                      if e["kind"] == "policy_changed_mid_batch")
        assert change["payload"]["from_version"] == base.version
        assert change["payload"]["to_version"] == updated.version
        assert change["payload"]["resume_from"] == 1

        # 已有挂牌身份与状态语义不变。
        restored = store.get(con, "a-1")
        assert restored["listing_identity"]["key"] == identity_before
        assert restored["listing_status"] == "active"
        assert store.checkpoint(con, "b-resume")["status"] == "completed"
    finally:
        con.close()


def test_identity_stable_for_listing_without_explicit_id():
    from src.listingtrail import identity

    first = identity.process({"src": "alpha", "url": "https://a.example/x",
                              "name": "X"})
    second = identity.process({"src": "alpha", "url": "https://a.example/x",
                               "name": "X", "policy_version": "v2",
                               "fetch_outcome": "blocked",
                               "fetch": {"http_status": 403}})
    assert first["listing_identity"]["key"] == second["listing_identity"]["key"]
    assert key_of({"id": "r-1", "src": "alpha", "url": "x"}) == "r-1"


def test_identity_without_id_or_url_ignores_runtime_fields():
    from src.listingtrail import identity, keyer

    base = {"src": "alpha", "name": "No URL", "value": 3}
    first = identity.process(base)
    second = identity.process(
        {**base, "policy_version": "v9", "fetch_outcome": "blocked",
         "failure_category": "access_denied", "resume_position": 7,
         "render_trace": {"request_kind": "render"}}
    )
    assert first["listing_identity"]["key"] == second["listing_identity"]["key"]
    # 不含采集运行期字段的记录，派生键与基线摘要完全一致。
    plain = {"src": "alpha", "name": "No URL", "rooms": 2}
    assert keyer.derived_key(plain) == keyer.key_of(plain)
    # 价格/状态变化属于历史，不是新挂牌。
    assert keyer.derived_key({**plain, "price": 100, "status": "active"}) == \
        keyer.derived_key({**plain, "price": 90, "status": "sold"})


def test_stored_identity_wins_over_new_derivation(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, resume=False, batch_id="b1", records=[
            {"src": "alpha", "url": "https://a.example/y", "name": "Y",
             "status": "active", "ts": "2026-11-01T00:00:00Z"}
        ])
        key = store.all_records(con)[0][0]
        # 后续观测即便多了字段，也沿用已落库主键（URL 是稳定身份依据）。
        pipeline.run(con=con, resume=False, batch_id="b2", records=[
            {"src": "alpha", "url": "https://a.example/y", "name": "Y",
             "status": "active", "extra": "new",
             "ts": "2026-11-02T00:00:00Z"}
        ])
        rows = store.all_records(con)
        assert len(rows) == 1
        stored = dict(rows)
        assert stored[key]["listing_identity"]["key"] == key
        assert stored[key]["listing_identity"]["stable_since"] == "2026-11-01T00:00:00Z"
    finally:
        con.close()


def test_blocked_records_are_traceable_in_db(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, resume=False, batch_id="b1", records=[
            listing("a-1", "alpha", fetch={"http_status": 403}),
            listing("b-1", "beta"),
        ])
        trace = store.fetch_trace(con, "b1")
        blocked = [t for t in trace if t["outcome"] == "blocked"]
        assert len(blocked) == 1
        assert blocked[0]["failure_category"] == "access_denied"
        assert blocked[0]["request_kind"] == "plain"
        assert blocked[0]["payload"]["escalation"]["next_request"] == "render"
        assert blocked[0]["payload"]["resume_position"] == 0

        versions = store.policy_versions(con)
        assert len(versions) == 1
        assert versions[0]["version"] == trace[0]["policy_version"]
    finally:
        con.close()


def test_baseline_happy_path_still_works(tmp_path):
    con = seeded(tmp_path)
    try:
        result = pipeline.run(con=con, records=[
            {"id": "r-001", "src": "alpha", "name": " Record One ", "value": 10,
             "status": "active", "ts": "2026-11-01T00:00:00Z"},
            {"id": "r-002", "src": "Beta", "name": "Record Two", "value": 20,
             "status": "active", "ts": "2026-11-02T00:00:00Z"},
            {"id": "r-003", "src": "alpha", "name": "Record Three", "value": 30,
             "status": "sold", "ts": "2026-11-03T00:00:00Z"},
            {"id": "r-003", "src": "alpha", "name": "Record Three", "value": 30,
             "status": "sold", "ts": "2026-11-03T00:00:00Z"},
        ], resume=False, batch_id="happy")
        assert result["fed"] == 4
        assert result["rows"] == 3
        assert store.get(con, "r-003")["listing_status"] == "sold"
    finally:
        con.close()


def test_breaker_state_survives_restart_via_checkpoint(tmp_path):
    db = tmp_path / "b.db"
    policy = fetch_policy.load_policies(
        {"alpha": {"circuit_breaker": {"failure_threshold": 1,
                                       "cooldown_seconds": 60.0,
                                       "allow_probe": True,
                                       "probe_after_seconds": 60.0}}}
    )
    feed = [
        listing("a-1", "alpha", fetch={"http_status": 503}),
        listing("a-2", "alpha"),
    ]
    clock = {"t": 0.0}
    con = store.connect(str(db))
    try:
        pipeline.run(con=con, records=feed, policies=policy, batch_id="brk",
                     resume=False, stop_after=1, now=lambda: clock["t"])
    finally:
        con.close()

    con = store.connect(str(db))
    try:
        pipeline.run(con=con, records=feed, policies=policy, resume=True,
                     now=lambda: clock["t"])
        trace = store.fetch_trace(con, "brk")
        # 熔断状态跨重启保留：恢复后的记录不再发普通请求，也不改状态。
        assert trace[1]["request_kind"] == "none"
        assert trace[1]["failure_category"] == "circuit_open"
        assert trace[1]["outcome"] == fetch_policy.OUTCOME_TEMPORARY
        restored = store.get(con, "a-1")
        assert restored["listing_status"] == "unknown"
        assert restored["listing_snapshot"]["credible"] is False
    finally:
        con.close()


def test_incompatible_resume_starts_new_batch_and_is_traceable(tmp_path):
    con = seeded(tmp_path)
    try:
        pipeline.run(con=con, resume=False, batch_id="b-old", stop_after=1,
                     records=[listing("a-1", "alpha"), listing("a-2", "alpha")])
        result = pipeline.run(
            con=con, resume=True, batch_id="b-new",
            records=[listing("a-1", "alpha")],
        )
        assert result["batch_id"] == "b-new"
        assert result["resume_from"] == 0
        kinds = [e["kind"] for e in store.events(con, "b-old")]
        assert "resume_incompatible" in kinds
    finally:
        con.close()


def test_render_mode_can_return_to_plain_after_hold():
    policies = fetch_policy.load_policies(
        {"alpha": {"render_on_block": True, "render_hold_records": 2}}
    )
    session = fetch_policy.Session(policies=policies, now=lambda: 0.0)
    blocked = session.observe(
        listing("a-1", "alpha", fetch={"http_status": 403}), position=0
    )
    assert blocked.escalation["hold_records"] == 2
    first = session.observe(listing("a-2", "alpha"), position=1)
    second = session.observe(listing("a-3", "alpha"), position=2)
    third = session.observe(listing("a-4", "alpha"), position=3)
    assert first.request_kind == "render"
    assert second.request_kind == "render"
    # 保持计数耗尽后回到普通请求，选择全程可追踪。
    assert third.request_kind == "plain"
    assert third.request_reason == "policy_plain"


def test_retry_plan_only_for_retryable_categories():
    policies = fetch_policy.load_policies(
        {"alpha": {"retry": {"max_attempts": 3, "backoff_seconds": 1.0}}}
    )
    session = fetch_policy.Session(policies=policies, now=lambda: 0.0)
    timeout = session.observe(
        listing("a-1", "alpha", fetch={"error": "read timeout", "attempts": 2}),
        position=0,
    )
    assert timeout.outcome == fetch_policy.OUTCOME_TEMPORARY
    assert timeout.failure_category == fetch_policy.CATEGORY_TIMEOUT
    assert timeout.retry_count == 1
    assert timeout.retry_delay_seconds == 2.0

    blocked = session.observe(
        listing("a-2", "alpha", fetch={"http_status": 403}), position=1
    )
    assert blocked.retry_count == 0
    assert blocked.retry_scheduled is False


def test_policy_config_file_overrides(tmp_path, monkeypatch):
    from src.listingtrail import config

    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"beta": {"rate_limit": {"min_interval_seconds": 7.0}}}),
                    encoding="utf-8")
    monkeypatch.setattr(config, "POLICY_CONFIG", path)
    policies = fetch_policy.load_policies(config.load_policy_overrides())
    assert policies.for_source("beta").rate_limit.min_interval_seconds == 7.0


def test_decide_and_run_batch_helpers():
    policies = fetch_policy.load_policies()
    single = fetch_policy.decide(
        listing("a-1", "alpha", fetch={"http_status": 403}),
        policies=policies, now=0.0, position=0, batch_id="h1",
    )
    assert single["outcome"] == fetch_policy.OUTCOME_BLOCKED
    assert single["decision_version"] == policies.version
    assert single["escalation"]["next_request"] == "render"

    decisions = fetch_policy.run_batch(
        [listing("a-1", "alpha"), listing("b-1", "beta")],
        policies=policies, now=lambda: 0.0, batch_id="h2",
    )
    assert [d["position"] for d in decisions] == [0, 1]
    assert {d["source"] for d in decisions} == {"alpha", "beta"}


def test_snapshot_and_status_helpers_agree():
    from src.listingtrail import statusline
    from src.listingtrail import snapshot as snapshot_mod

    clean = {"fetch_outcome": "ok", "listing_status": "delisted",
             "listing_snapshot": {"credible": True}}
    assert statusline.is_delisting_fact(clean) is True
    assert snapshot_mod.is_degraded(clean) is False

    degraded = {"fetch_outcome": "temporarily_unavailable",
                "listing_status": "delisted",
                "listing_snapshot": {"credible": False}}
    assert statusline.is_delisting_fact(degraded) is False
    assert snapshot_mod.is_degraded(degraded) is True

    empty = {"fetch_outcome": "empty_result", "listing_status": "active",
             "listing_snapshot": {"credible": True}}
    assert statusline.is_delisting_fact(empty) is False


def test_stage_contracts_are_single_purpose():
    """各阶段可独立调用，且决策字段透传到下游。"""
    policies = fetch_policy.load_policies()
    rec = pipeline._load_chain()
    names = [name for name, _ in rec]
    assert names == ["normalizer", "fetch_policy", "identity", "renderer",
                     "statusline", "snapshot"]

    from src.listingtrail import normalizer, identity, renderer, statusline, snapshot
    record = normalizer.process(listing("a-1", "alpha"))
    record = fetch_policy.process(record, None)
    assert record["decision_version"] == policies.version
    record = identity.process(record)
    record = renderer.process(record)
    record = statusline.process(record)
    record = snapshot.process(record)
    assert record["listing_snapshot"]["policy_version"] == policies.version
    assert record["listing_snapshot"]["resume_position"] == 0
    assert record["status_history"]
