"""动态房源页渲染后抽取编排的离线测试。

覆盖：首屏、异步接口、空壳、验证码/失败页；等待超时；局部成功；重复渲染；
缓存失效与恢复；重启后挂牌行数与错误分类；并确认原有成功抽取接口不变。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from src.listingtrail import fetch_policy, pipeline, renderer, snapshot, store

PAGES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pages.json"


def load_pages():
    return json.loads(PAGES_FIXTURE.read_text(encoding="utf-8"))


def fresh_con(tmp_path):
    con = sqlite3.connect(tmp_path / "listings.db")
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con


@pytest.fixture(autouse=True)
def _clear_module_cache():
    renderer.reset_cache()
    yield
    renderer.reset_cache()


# ---------------------------------------------------------------------------
# fetch_policy：抓取方式与等待/缓存边界
# ---------------------------------------------------------------------------

def test_policy_resolves_dynamic_for_page_sequences_and_static_on_hint():
    dynamic = fetch_policy.resolve({"id": "p", "pages": [{"kind": "first_screen"}]})
    assert dynamic["method"] == fetch_policy.METHOD_DYNAMIC

    static = fetch_policy.resolve({"id": "p", "fetch_method": "static", "pages": [{"kind": "first_screen"}]})
    assert static["method"] == fetch_policy.METHOD_STATIC

    explicit = fetch_policy.resolve({"id": "p", "fetch_policy": {"method": "static"}})
    assert explicit["method"] == fetch_policy.METHOD_STATIC


def test_policy_normalises_wait_and_cache_boundaries():
    policy = fetch_policy.normalize_policy({"method": "dynamic", "timeout_s": 3, "retries": "2", "cache_ttl_ms": "500"})
    assert policy["timeout_ms"] == 3000
    assert policy["retries"] == 2
    assert policy["cache_ttl_ms"] == 500
    assert policy["on_partial"] == fetch_policy.ON_PARTIAL_KEEP


def test_policy_stage_preserves_legacy_marker():
    rec = fetch_policy.process({"id": "p"})
    assert rec["_stage_fetch_policy"] == "ok"
    assert rec["fetch_method"] in fetch_policy.METHODS


# ---------------------------------------------------------------------------
# renderer：首屏 / 异步 / 空壳 / 验证码 / 失败页
# ---------------------------------------------------------------------------

def test_first_screen_then_async_success_is_complete():
    result = renderer.render_page(load_pages()[0])
    assert result["status"] == renderer.STATUS_COMPLETE
    assert result["page_kind"] == renderer.KIND_ASYNC
    assert result["error_kind"] is None
    assert [item["id"] for item in result["listings"]] == ["alpha-1001", "alpha-1002"]
    assert result["waited_ms"] == 300
    assert result["budget_exceeded"] is False


def test_http_200_empty_shell_and_captcha_are_not_listings():
    captcha = renderer.render_page(load_pages()[2])
    assert captcha["status"] == renderer.STATUS_FAILED
    assert captcha["page_kind"] == renderer.KIND_CAPTCHA
    assert captcha["error_kind"] == renderer.ERROR_CAPTCHA
    assert captcha["listings"] == []

    shell = renderer.render_page(load_pages()[3])
    assert shell["status"] == renderer.STATUS_FAILED
    assert shell["page_kind"] == renderer.KIND_EMPTY_SHELL
    assert shell["error_kind"] == renderer.ERROR_EMPTY_SHELL
    assert shell["listings"] == []

    http_error = renderer.render_page(load_pages()[5])
    assert http_error["status"] == renderer.STATUS_FAILED
    assert http_error["page_kind"] == renderer.KIND_ERROR
    assert http_error["error_kind"] == renderer.ERROR_HTTP


def test_wait_timeout_with_first_screen_keeps_partial_listings():
    result = renderer.render_page(load_pages()[1])
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["budget_exceeded"] is True
    assert result["waited_ms"] == 500           # 等待预算封顶，不会超过 timeout_ms
    assert result["error_kind"] == renderer.ERROR_TIMEOUT
    # 局部成功：保留首屏可用字段，并让下游可见 partial 标记。
    assert result["partial"] is True
    assert [item["id"] for item in result["listings"]] == ["alpha-2001"]


def test_empty_shell_then_async_listings_dedupes_duplicate_renders():
    result = renderer.render_page(load_pages()[4])
    assert result["status"] == renderer.STATUS_COMPLETE
    ids = [item["id"] for item in result["listings"]]
    assert ids == ["beta-3001", "beta-3002"]     # beta-3002 重复渲染被去重


def test_partial_retry_policy_retries_whole_page_but_keeps_usable_fields():
    record = {
        "id": "retry-page",
        "fetch_policy": {"timeout_ms": 400, "on_partial": "retry", "retries": 1},
        "page_attempts": [
            [{"kind": "first_screen", "listings": [{"id": "keep-1"}]},
             {"kind": "async", "wait_ms": 900}],
            [{"kind": "first_screen", "listings": [{"id": "keep-1"}]},
             {"kind": "async", "wait_ms": 900}],
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["attempts"] == 2
    assert result["retry_failed"] is True
    assert [item["id"] for item in result["listings"]] == ["keep-1"]


def test_async_expected_but_static_method_is_partial_not_complete():
    record = {
        "id": "static-page",
        "fetch_method": "static",
        "pages": [
            {"kind": "first_screen", "listings": [{"id": "s-1"}]},
            {"kind": "async", "data": {"listings": [{"id": "s-2"}]}},
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["async_skipped"] is True
    assert result["partial_reason"] == renderer.PARTIAL_ASYNC
    assert [item["id"] for item in result["listings"]] == ["s-1"]


def test_render_is_idempotent_across_repeated_calls():
    record = load_pages()[0]
    first = renderer.render_page(record)
    second = renderer.render_page(record)
    assert [x["id"] for x in first["listings"]] == [x["id"] for x in second["listings"]]
    assert len(second["listings"]) == len({x["id"] for x in second["listings"]})


def test_data_listing_attributes_and_json_payloads_are_extracted():
    html = (
        "<div data-listing-id='h-1' data-title='阳光房' data-price='100'></div>"
        "<div data-listing-id='h-2' data-title='地铁房'></div>"
    )
    result = renderer.render_page({"id": "h", "pages": [{"kind": "first_screen", "html": html}]})
    assert result["status"] == renderer.STATUS_COMPLETE
    assert [item["id"] for item in result["listings"]] == ["h-1", "h-2"]


def test_classify_page_distinguishes_shell_and_captcha():
    assert renderer.classify_page({"status": 200, "html": "<div id='root'>加载中</div>"}) == renderer.KIND_EMPTY_SHELL
    assert renderer.classify_page({"status": 200, "html": "请完成验证码"}) == renderer.KIND_CAPTCHA
    assert renderer.classify_page({"kind": "error", "status": 500}) == renderer.KIND_ERROR


# ---------------------------------------------------------------------------
# 缓存：确认边界、失效与恢复
# ---------------------------------------------------------------------------

def test_cache_hit_avoids_rerender_and_expiry_refreshes():
    record = load_pages()[0] | {"url": "cache-page", "use_cache": True, "cache": {"ttl_ms": 1000}}
    cache = {}

    first = renderer.render_page(record, cache=cache, now_ms=1000)
    assert first["status"] == renderer.STATUS_COMPLETE
    assert first["cache_state"] == renderer.CACHE_STORED

    hit = renderer.render_page(record, cache=cache, now_ms=1500)
    assert hit["cache_state"] == renderer.CACHE_HIT
    assert hit["cache_age_ms"] == 500
    assert hit["attempts"] == 0                     # 命中缓存不再渲染
    assert [x["id"] for x in hit["listings"]] == ["alpha-1001", "alpha-1002"]

    expired = renderer.render_page(record, cache=cache, now_ms=5000)
    assert expired["status"] == renderer.STATUS_COMPLETE
    assert expired["cache_state"] == renderer.CACHE_REFRESHED


def test_cache_is_invalidated_when_rerender_fails():
    record = load_pages()[0] | {"url": "cache-fail", "use_cache": True, "cache": {"ttl_ms": 1000}}
    cache = {}
    renderer.render_page(record, cache=cache, now_ms=1000)
    assert "cache-fail" in cache

    broken = dict(record, pages=[{"kind": "captcha", "html": "验证码"}])
    result = renderer.render_page(broken, cache=cache, now_ms=9000)
    assert result["status"] == renderer.STATUS_FAILED
    assert result["cache_state"] == renderer.CACHE_INVALIDATED
    assert "cache-fail" not in cache            # 错误页不会污染缓存


def test_cache_fallback_recovers_last_good_snapshot():
    record = load_pages()[0] | {"url": "cache-recover", "use_cache": True, "cache": {"ttl_ms": 1000}}
    cache = {}
    renderer.render_page(record, cache=cache, now_ms=1000)

    broken = dict(
        record,
        fetch_policy={"cache_fallback": True},
        pages=[{"kind": "timeout", "wait_ms": 99999}],
    )
    recovered = renderer.render_page(broken, cache=cache, now_ms=5000)
    assert recovered["status"] == renderer.STATUS_COMPLETE
    assert recovered["cache_state"] == renderer.CACHE_STALE
    assert recovered["recovered"] is True
    assert recovered["failed_error_kind"] == renderer.ERROR_TIMEOUT
    assert len(recovered["listings"]) == 2


def test_explicit_cache_invalidation_forces_refetch():
    record = load_pages()[0] | {"url": "cache-invalidate", "use_cache": True, "cache": {"ttl_ms": 10_000}}
    cache = {}
    renderer.render_page(record, cache=cache, now_ms=1000)

    forced = dict(record, cache={"ttl_ms": 10_000, "valid": False})
    result = renderer.render_page(forced, cache=cache, now_ms=1100)
    assert result["cache_state"] == renderer.CACHE_REFRESHED
    assert result["status"] == renderer.STATUS_COMPLETE


# ---------------------------------------------------------------------------
# snapshot：完成状态向下游传播；错误页不产生行
# ---------------------------------------------------------------------------

def test_snapshot_marks_partial_rows_and_drops_failed_pages():
    partial = renderer.render_page(load_pages()[1])
    snap = snapshot.build(partial, source="alpha", page_id="page-002")
    assert snap["status"] == renderer.STATUS_PARTIAL
    assert snap["row_count"] == 1
    assert snap["rows"][0]["render_partial"] is True
    assert snap["rows"][0]["render_complete"] is False
    assert snap["rows"][0]["error_kind"] == renderer.ERROR_TIMEOUT
    assert snap["rows"][0]["src"] == "alpha"

    failed = renderer.render_page(load_pages()[2])
    assert snapshot.build(failed)["rows"] == []
    assert snapshot.build(failed)["valid"] is False


# ---------------------------------------------------------------------------
# 离线流水线：行数、重启、错误分类
# ---------------------------------------------------------------------------

def test_offline_page_sequence_pipeline_row_counts_and_classification(tmp_path):
    con = fresh_con(tmp_path)
    try:
        first = pipeline.run_records(load_pages(), con=con)
        assert first["fed"] == 6
        # page-001: 2, page-002: 1, page-005: 2；错误页 0 行。
        assert first["rows"] == 5
        assert first["emitted"] == 5

        statuses = {r["key"]: r["status"] for r in first["records"]}
        assert statuses["page-001"] == renderer.STATUS_COMPLETE
        assert statuses["page-002"] == renderer.STATUS_PARTIAL
        assert statuses["page-003"] == renderer.STATUS_FAILED
        assert statuses["page-004"] == renderer.STATUS_FAILED
        assert statuses["page-005"] == renderer.STATUS_COMPLETE
        assert statuses["page-006"] == renderer.STATUS_FAILED

        errors = {r["key"]: r["error_kind"] for r in first["records"] if r["status"] == renderer.STATUS_FAILED}
        assert errors == {
            "page-003": renderer.ERROR_CAPTCHA,
            "page-004": renderer.ERROR_EMPTY_SHELL,
            "page-006": renderer.ERROR_HTTP,
        }

        # 重复渲染（同一批记录再跑一次）不增行。
        second = pipeline.run_records(load_pages(), con=con)
        assert second["rows"] == first["rows"] == 5
    finally:
        con.close()


def test_restart_reuses_persisted_rows_and_cache(tmp_path):
    db_path = tmp_path / "persisted.db"
    cache = {}

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    first = pipeline.run_records(load_pages(), con=con, cache=cache)
    con.commit()
    con.close()

    # 模拟重启：新的连接 + 同一份缓存字典。
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    try:
        second = pipeline.run_records(load_pages(), con=con, cache=cache)
        assert second["rows"] == first["rows"] == 5
        ids = {row["id"] for _, row in store.all_records(con)}
        assert ids == {"alpha-1001", "alpha-1002", "alpha-2001", "beta-3001", "beta-3002"}
    finally:
        con.close()


def test_cached_page_turning_into_captcha_keeps_rows_and_classifies(tmp_path):
    """重启后同一页面变成验证码：挂牌行数不增，错误被分类而不是当成有效挂牌。"""
    db_path = tmp_path / "restart.db"
    cache = {}
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    try:
        page = load_pages()[0] | {
            "id": "restart-page",
            "url": "https://alpha.example/restart",
            "use_cache": True,
            "cache": {"ttl_ms": 1000},
            "now_ms": 1000,
        }
        first = pipeline.run_records([page], con=con, cache=cache)
        assert first["rows"] == 2

        captcha = dict(
            page,
            pages=[{"kind": "captcha", "status": 200, "html": "请完成验证码"}],
            now_ms=99_999,
        )
        second = pipeline.run_records([captcha], con=con, cache=cache)
        assert second["rows"] == 2                     # 不增行，也不删旧行
        assert second["records"][0]["status"] == renderer.STATUS_FAILED
        assert second["records"][0]["error_kind"] == renderer.ERROR_CAPTCHA
        assert cache == {}                             # 错误页让缓存失效
    finally:
        con.close()


def test_cumulative_waits_are_bounded_by_timeout():
    record = {
        "id": "budget",
        "fetch_policy": {"timeout_ms": 400},
        "pages": [
            {"kind": "first_screen", "listings": [{"id": "b-1"}]},
            {"kind": "async", "wait_ms": 250, "data": {"listings": [{"id": "b-2"}]}},
            {"kind": "async", "wait_ms": 250, "data": {"listings": [{"id": "b-3"}]}},
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["budget_exceeded"] is True
    assert result["waited_ms"] == 400                  # 封顶在预算内
    ids = [x["id"] for x in result["listings"]]
    assert ids == ["b-1", "b-2"]                       # 超预算的那一步没有执行


def test_original_success_extraction_interface_unchanged(tmp_path):
    records = [
        {"id": "r-001", "src": "alpha", "name": " Record One ", "value": 10},
        {"id": "r-002", "src": "Beta", "name": "Record Two ", "value": 20},
    ]
    con = fresh_con(tmp_path)
    try:
        out = pipeline.run_records(records, con=con)
        assert out["fed"] == 2
        assert out["rows"] == 2
    finally:
        con.close()

    # 原有 process(record) 语义保留：普通记录只补阶段标记。
    rec = renderer.process({"id": "plain", "name": "x"})
    assert rec["_stage_renderer"] == "naive"
    assert "extraction" not in rec

    rec = renderer.process({"id": "page", "pages": [{"kind": "first_screen", "listings": [{"id": "L1"}]}]})
    assert rec["_stage_renderer"] == "naive"
    assert rec["render_status"] == renderer.STATUS_COMPLETE
    assert rec["extraction"]["listings"][0]["id"] == "L1"

    snap = snapshot.process(rec)
    assert snap["_stage_snapshot"] == "naive"
    assert snap["snapshot"]["row_count"] == 1
    assert snap["_emit"][0]["id"] == "L1"


def test_partial_then_blocked_keeps_fields_and_reports_blocked():
    record = {
        "id": "blocked-midway",
        "pages": [
            {"kind": "first_screen", "listings": [{"id": "keep-1"}, {"id": "keep-2"}]},
            {"kind": "captcha", "html": "请完成验证码"},
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["blocked"] is True
    assert result["error_kind"] == renderer.ERROR_CAPTCHA
    assert result["partial_reason"] == renderer.PARTIAL_BLOCKED
    assert [x["id"] for x in result["listings"]] == ["keep-1", "keep-2"]


def test_async_empty_payload_without_first_screen_is_failed():
    record = {"id": "empty-async", "pages": [{"kind": "async", "data": {"listings": []}}]}
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_FAILED
    assert result["listings"] == []
    assert result["error_kind"] == renderer.ERROR_EMPTY_SHELL


def test_async_endpoint_error_after_first_screen_is_partial_and_blocked():
    record = {
        "id": "async-blocked",
        "pages": [
            {"kind": "first_screen", "listings": [{"id": "s-1"}]},
            {"kind": "async", "status": 403, "data": {"message": "forbidden"}},
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["blocked"] is True
    assert result["error_kind"] == renderer.ERROR_BLOCKED
    assert [x["id"] for x in result["listings"]] == ["s-1"]


def test_page_attempts_retry_can_escalate_to_complete():
    record = {
        "id": "retry-upgrade",
        "fetch_policy": {"retries": 1, "timeout_ms": 300},
        "page_attempts": [
            [{"kind": "empty_shell"}],
            [{"kind": "async", "data": {"listings": [{"id": "late-1"}]}}],
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_COMPLETE
    assert result["attempts"] == 2
    assert [x["id"] for x in result["listings"]] == ["late-1"]


def test_alternate_phase_shape_is_supported():
    record = {
        "id": "phases",
        "first_screen": {"listings": [{"id": "p-1"}]},
        "async": {"wait_ms": 100, "data": {"listings": [{"id": "p-2"}]}},
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_COMPLETE
    assert result["waited_ms"] == 100
    assert [x["id"] for x in result["listings"]] == ["p-1", "p-2"]


def test_required_fields_downgrade_complete_to_partial():
    record = {
        "id": "fields",
        "required_fields": ["price", "community"],
        "pages": [
            {"kind": "first_screen", "listings": [{"id": "f-1", "price": 1}]},
        ],
    }
    result = renderer.render_page(record)
    assert result["status"] == renderer.STATUS_PARTIAL
    assert result["partial_reason"] == renderer.PARTIAL_FIELDS
    assert "community" in result["missing_fields"]
