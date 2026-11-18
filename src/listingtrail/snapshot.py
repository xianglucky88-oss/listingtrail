"""单一职责：把 renderer 的抽取结果固化为快照行，并向下游传递完成状态。

本模块不抓取、不渲染，只做两件事：

1. 根据 ``extraction.status``（``complete`` / ``partial`` / ``failed``）决定
   哪些房源可以进入快照。``failed`` 页面（空壳、验证码、超时、错误页）
   **不会**产生任何挂牌行，避免把错误页当成有效房源。
2. 为每一行补齐稳定主键、来源和完成状态，供 ``keyer`` / ``store`` 幂等落库。

``process(record)`` 对不含页面负载的普通记录保持原有朴素行为（仅补
``_stage_snapshot`` 标记），因此原有 happy-path 接口不变。页面记录则在
``record["snapshot"]`` 中给出带完成状态的快照，并通过 ``record["_emit"]``
向编排层提供需要落库的行。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

from . import renderer as _renderer

STATUS_COMPLETE = _renderer.STATUS_COMPLETE
STATUS_PARTIAL = _renderer.STATUS_PARTIAL
STATUS_FAILED = _renderer.STATUS_FAILED


def _row_key(listing: Mapping[str, Any], index: int) -> str:
    key = _renderer.listing_key(listing, index)
    if key is None:
        return "row-%d" % index
    return key


def _source_of(record: Mapping[str, Any]) -> str | None:
    for key in ("src", "source", "site", "origin"):
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _page_id_of(record: Mapping[str, Any]) -> str | None:
    for key in ("page_id", "url", "id"):
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def rows_from_extraction(
    extraction: Mapping[str, Any],
    *,
    source: str | None = None,
    page_id: str | None = None,
    fetched_at: Any = None,
) -> list[dict[str, Any]]:
    """Return persistable rows for the listings in *extraction*.

    Failed extractions return ``[]``; partial extractions return the usable
    subset and mark every row with ``partial=True`` so downstream consumers can
    tell "字段可用但页面未完成" from a fully successful extraction.
    """
    status = extraction.get("status")
    if status not in (STATUS_COMPLETE, STATUS_PARTIAL):
        return []
    listings = extraction.get("listings") or []
    if not isinstance(listings, list):
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, listing in enumerate(listings):
        if not isinstance(listing, Mapping):
            continue
        row = {
            str(k): copy.deepcopy(v)
            for k, v in listing.items()
            if not str(k).startswith("_")
        }
        key = _row_key(row, index)
        if key in seen:
            continue
        seen.add(key)
        row.setdefault("id", key)
        row["render_status"] = status
        row["render_complete"] = status == STATUS_COMPLETE
        row["render_partial"] = status == STATUS_PARTIAL
        row["page_kind"] = extraction.get("page_kind")
        row["error_kind"] = extraction.get("error_kind")
        row["partial_reason"] = extraction.get("partial_reason")
        if source is not None:
            row.setdefault("source", source)
            row.setdefault("src", source)
        if page_id is not None:
            row.setdefault("page_id", page_id)
        if fetched_at is not None:
            row.setdefault("fetched_at", fetched_at)
        rows.append(row)
    return rows


def build(
    extraction: Mapping[str, Any],
    *,
    source: str | None = None,
    page_id: str | None = None,
    fetched_at: Any = None,
) -> dict[str, Any]:
    """Build the snapshot object carried on a rendered record."""
    status = extraction.get("status", STATUS_FAILED)
    rows = rows_from_extraction(
        extraction, source=source, page_id=page_id, fetched_at=fetched_at
    )
    if status == STATUS_COMPLETE and not rows:
        # 完成状态却没有任何行，属于不一致输入：降级为失败，避免空快照冒充成功。
        status = STATUS_FAILED
        extraction = dict(extraction, status=STATUS_FAILED)
    error_kind = extraction.get("error_kind")
    if status == STATUS_FAILED and not error_kind:
        error_kind = _renderer.ERROR_NO_LISTINGS
    return {
        "status": status,
        "render_status": extraction.get("status"),
        "complete": status == STATUS_COMPLETE,
        "partial": status == STATUS_PARTIAL,
        "valid": bool(rows),
        "page_kind": extraction.get("page_kind"),
        "error_kind": error_kind,
        "partial_reason": extraction.get("partial_reason"),
        "missing_fields": list(extraction.get("missing_fields") or []),
        "cache_state": extraction.get("cache_state"),
        "attempts": extraction.get("attempts", 0),
        "waited_ms": extraction.get("waited_ms", 0),
        "budget_exceeded": extraction.get("budget_exceeded", False),
        "rows": rows,
        "row_count": len(rows),
    }


def process(record: Mapping[str, Any]) -> dict[str, Any]:
    """Pipeline stage: snapshot the renderer result and expose rows to store."""
    rec = dict(record)
    rec.setdefault("_stage_snapshot", "naive")

    extraction = rec.get("extraction")
    if not isinstance(extraction, Mapping):
        if not _renderer.has_page_payload(rec):
            return rec
        cache = rec.get("cache_store") if isinstance(rec.get("cache_store"), dict) else None
        extraction = _renderer.render_page(rec, cache=cache)
        rec["extraction"] = extraction

    snapshot = build(
        extraction,
        source=_source_of(rec),
        page_id=_page_id_of(rec),
        fetched_at=rec.get("fetched_at") or rec.get("ts"),
    )
    rec["snapshot"] = snapshot
    rec["render_status"] = snapshot["status"]
    rec["page_kind"] = snapshot["page_kind"]
    rec["error_kind"] = snapshot["error_kind"]
    rec["render_complete"] = snapshot["complete"]
    rec["render_partial"] = snapshot["partial"]
    rec["_emit"] = snapshot["rows"]
    return rec


def summary(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate snapshot statuses / error kinds for reporting and assertions."""
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    page_kinds: dict[str, int] = {}
    rows = 0
    for record in records:
        snap = record.get("snapshot") if isinstance(record, Mapping) else None
        if not isinstance(snap, Mapping):
            continue
        status = str(snap.get("status"))
        statuses[status] = statuses.get(status, 0) + 1
        page_kind = snap.get("page_kind")
        if page_kind:
            page_kinds[str(page_kind)] = page_kinds.get(str(page_kind), 0) + 1
        error = snap.get("error_kind")
        if status == STATUS_FAILED and error:
            errors[str(error)] = errors.get(str(error), 0) + 1
        rows += int(snap.get("row_count") or 0)
    return {
        "records": sum(statuses.values()),
        "rows": rows,
        "statuses": statuses,
        "errors": errors,
        "page_kinds": page_kinds,
    }


__all__ = [
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_FAILED",
    "rows_from_extraction",
    "build",
    "process",
    "summary",
]
