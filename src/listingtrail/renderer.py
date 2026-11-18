"""单一职责：渲染页面并抽取房源，输出带完成状态的抽取结果。

本阶段接收 ``fetch_policy`` 给出的抓取方式（``static`` / ``dynamic``），
按离线页面序列模拟"首屏 → 等待异步接口 → 抽取"的编排，并把*完成状态*
显式交给下游（``snapshot``）：

``status``
    ``complete``  页面内容完整，房源与字段均已取得；
    ``partial``   页面局部成功：保留了可用房源/字段，但仍缺少异步内容或字段；
    ``failed``    未取得任何有效房源。

``page_kind`` / ``error_kind``
    区分首屏（``first_screen``）、异步接口（``async``）、空壳页面
    （``empty_shell``）、验证码（``captcha``）、等待超时（``timeout``）和
    失败页（``error``）。HTTP 200 的空壳/验证码页不会被当成有效挂牌。

等待与缓存边界
    等待预算来自 policy 的 ``timeout_ms``；每个异步步骤声明 ``wait_ms``，
    累计超预算立即判为超时。缓存以 ``cache_ttl_ms`` 为硬边界，过期即失效；
    失效后重新渲染并回填缓存即恢复。``render()`` 是幂等的：同一页面序列
    重复渲染不会产生重复房源。

离线页面序列
    ``record["pages"]`` / ``record["page_sequence"]`` 为步骤列表；若一次渲染
    需要多轮尝试，可用 ``record["page_attempts"]`` 给出"每轮一组步骤"。
    步骤示例::

        {"kind": "first_screen", "html": "...", "listings": [...]}
        {"kind": "async", "wait_ms": 300, "listings": [...]}
        {"kind": "captcha", "html": "验证码 ..."}
        {"kind": "timeout"}

原有成功抽取接口保留：``process(record)`` 对不含页面负载的普通记录仍是
朴素变换，只是额外标注 ``_stage_renderer``，不影响既有 happy-path。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any, Iterable, Mapping

from . import fetch_policy as _fetch_policy

# 完成状态
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUSES = (STATUS_COMPLETE, STATUS_PARTIAL, STATUS_FAILED)

# 页面/错误分类
KIND_FIRST_SCREEN = "first_screen"
KIND_ASYNC = "async"
KIND_EMPTY_SHELL = "empty_shell"
KIND_CAPTCHA = "captcha"
KIND_TIMEOUT = "timeout"
KIND_ERROR = "error"
PAGE_KINDS = (
    KIND_FIRST_SCREEN,
    KIND_ASYNC,
    KIND_EMPTY_SHELL,
    KIND_CAPTCHA,
    KIND_TIMEOUT,
    KIND_ERROR,
)

ERROR_EMPTY_SHELL = "empty_shell"
ERROR_CAPTCHA = "captcha"
ERROR_TIMEOUT = "timeout"
ERROR_HTTP = "http_error"
ERROR_BLOCKED = "blocked"
ERROR_PARSE = "parse_error"
ERROR_NO_LISTINGS = "no_listings"
ERROR_UNKNOWN = "unknown"

PARTIAL_ASYNC = "async_unavailable"
PARTIAL_FIELDS = "missing_fields"
PARTIAL_PARSE = "parse_error"
PARTIAL_BLOCKED = "blocked"

# 缓存状态（供下游识别缓存是否被确认/失效/恢复）
CACHE_DISABLED = "disabled"
CACHE_MISS = "miss"
CACHE_HIT = "hit"
CACHE_STALE = "stale"
CACHE_INVALIDATED = "invalidated"
CACHE_STORED = "stored"
CACHE_REFRESHED = "refreshed"

# 默认模块级缓存；调用方也可通过 record["cache_store"] 注入，便于模拟重启。
_DEFAULT_CACHE: dict[str, dict[str, Any]] = {}

_LISTING_ID_KEYS = ("id", "listing_id", "listingId", "code", "url", "link")
_LISTING_KEYS = ("listings", "items", "records", "results", "houses", "properties")

_CAPTCHA_MARKERS = (
    "captcha",
    "验证码",
    "are you a robot",
    "verify you are human",
    "访问过于频繁",
    "安全检查",
    "geetest",
    "cf-challenge",
    "challenge-platform",
)
_ERROR_MARKERS = (
    "access denied",
    "403 forbidden",
    "404 not found",
    "not found",
    "页面不存在",
    "访问出错",
    "系统繁忙",
    "服务不可用",
    "error page",
)
_SHELL_MARKERS = (
    "loading",
    "加载中",
    "请稍候",
    "skeleton",
    "placeholder",
    "app-root",
    "__next",
    "id=\"root\"",
    "id='root'",
)

_DATA_LISTING_RE = re.compile(r"<[^>]*\bdata-listing-id\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_ATTR_RE = re.compile(r"\bdata-([a-zA-Z0-9_-]+)\s*=\s*[\"']([^\"']*)[\"']", re.I)
_JSON_SCRIPT_RE = re.compile(
    r"<script[^>]*type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _text_of(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in ("html", "body", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def _lower_join(*parts: Any) -> str:
    chunks = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part.lower())
        elif isinstance(part, Mapping):
            chunks.append(json.dumps(part, ensure_ascii=False).lower())
    return "\n".join(chunks)


def _looks_captcha(text: str) -> bool:
    return any(marker in text for marker in _CAPTCHA_MARKERS)


def _looks_error(text: str) -> bool:
    return any(marker in text for marker in _ERROR_MARKERS)


def _looks_shell(text: str) -> bool:
    return any(marker in text for marker in _SHELL_MARKERS)


def _clean_listing(listing: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(k): copy.deepcopy(v)
        for k, v in listing.items()
        if not str(k).startswith("_")
    }


def listing_key(listing: Mapping[str, Any], index: int = 0) -> str | None:
    """Return a stable listing key, or ``None`` when the item is not a listing."""
    for key in _LISTING_ID_KEYS:
        value = listing.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    title = listing.get("title") or listing.get("name")
    if title is not None and str(title).strip():
        digest = hashlib.sha256(
            json.dumps(listing, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        return "h-" + digest
    return None


def _dedupe(listings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(listings):
        if not isinstance(raw, Mapping):
            continue
        item = _clean_listing(raw)
        key = listing_key(item, index)
        if key is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        item.setdefault("id", key)
        result.append(item)
    return result


def _find_listings(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    """Recursively find a list of listing objects inside a JSON-ish payload."""
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        found: list[Mapping[str, Any]] = []
        for key in _LISTING_KEYS:
            items = value.get(key)
            if isinstance(items, list):
                objects = [item for item in items if isinstance(item, Mapping)]
                if objects:
                    found.extend(objects)
        if found:
            return found
        for child in value.values():
            found.extend(_find_listings(child, depth + 1))
        return found
    if isinstance(value, list):
        found = []
        objects = [item for item in value if isinstance(item, Mapping)]
        if objects and all(listing_key(obj) for obj in objects):
            return objects
        for child in value:
            found.extend(_find_listings(child, depth + 1))
        return found
    return []


def _listings_from_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return _dedupe([item for item in payload if isinstance(item, Mapping)])
    if isinstance(payload, Mapping):
        direct = payload.get("listings")
        if isinstance(direct, list) and direct:
            return _dedupe([item for item in direct if isinstance(item, Mapping)])
        return _dedupe(_find_listings(payload))
    if isinstance(payload, str):
        return _listings_from_html(payload)
    return []


def _listings_from_html(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    listings: list[dict[str, Any]] = []
    for match in _DATA_LISTING_RE.finditer(html):
        tag = match.group(0)
        attrs = {"id": match.group(1)}
        for key, value in _ATTR_RE.findall(tag):
            key = key.replace("-", "_")
            if key == "listing_id":
                key = "id"
            attrs[key] = value
        listings.append(attrs)
    if listings:
        return _dedupe(listings)

    # 现代前端常把数据塞进 __NEXT_DATA__ / JSON-LD 脚本。
    for script in _JSON_SCRIPT_RE.findall(html):
        try:
            payload = json.loads(script.strip())
        except (TypeError, ValueError):
            continue
        found = _find_listings(payload)
        if found:
            listings.extend(found)
    return _dedupe(listings)


def _step_payload(step: Any) -> Any:
    if isinstance(step, str):
        return step
    if not isinstance(step, Mapping):
        return None
    for key in ("payload", "data", "json", "body", "html", "text", "content"):
        if key in step and step[key] is not None:
            return step[key]
    return step


def _step_kind(step: Any) -> str:
    if isinstance(step, str):
        text = step.lower()
        if _looks_captcha(text):
            return KIND_CAPTCHA
        return KIND_FIRST_SCREEN
    if not isinstance(step, Mapping):
        return KIND_ERROR

    explicit = step.get("kind") or step.get("type") or step.get("state") or step.get("phase")
    if explicit:
        name = str(explicit).strip().lower()
        if name in {"first_screen", "first", "initial", "static", "html", "ssr"}:
            return KIND_FIRST_SCREEN
        if name in {"async", "async_response", "api", "xhr", "response", "json", "hydration", "networkidle"}:
            return KIND_ASYNC
        if name in {"shell", "empty_shell", "empty", "skeleton", "placeholder", "loading"}:
            return KIND_EMPTY_SHELL
        if name in {"captcha", "challenge", "blocked", "verify", "antibot", "robot"}:
            return KIND_CAPTCHA
        if name in {"timeout", "wait_timeout", "slow", "deadline"}:
            return KIND_TIMEOUT
        if name in {"error", "failed", "http_error", "not_found", "denied", "http_500"}:
            return KIND_ERROR

    status = _as_int(step.get("status") or step.get("status_code"), 200)
    if status == 429:
        return KIND_CAPTCHA
    if status in (401, 403):
        return KIND_ERROR
    if status >= 400:
        return KIND_ERROR
    if step.get("timeout") is True or step.get("timed_out") is True:
        return KIND_TIMEOUT
    if step.get("captcha") is True or step.get("challenge") is True:
        return KIND_CAPTCHA
    if step.get("error"):
        return KIND_ERROR
    if step.get("shell") is True or step.get("empty") is True:
        return KIND_EMPTY_SHELL

    payload = _step_payload(step)
    listings = _listings_from_payload(payload)
    if listings:
        return KIND_ASYNC if step.get("async") is True else KIND_FIRST_SCREEN

    text = _lower_join(_text_of(payload), step.get("title"), step.get("message"))
    if _looks_captcha(text):
        return KIND_CAPTCHA
    if _looks_error(text):
        return KIND_ERROR
    if _looks_shell(text):
        return KIND_EMPTY_SHELL
    if step.get("async") is True:
        return KIND_ASYNC
    return KIND_FIRST_SCREEN


def classify_page(payload: Any) -> str:
    """Classify a single page payload into one of :data:`PAGE_KINDS`.

    Useful for callers that only have a captured page and no page sequence.
    """
    step = payload if isinstance(payload, Mapping) else {"payload": payload}
    return _step_kind(step)


def _step_wait_ms(step: Any) -> int:
    if not isinstance(step, Mapping):
        return 0
    for key in ("wait_ms", "delay_ms", "latency_ms", "wait"):
        if key in step:
            value = step[key]
            if key == "wait":
                return _as_int(value, 0) * 1000
            return _as_int(value, 0)
    return 0


def _page_attempts(record: Mapping[str, Any]) -> list[list[Any]]:
    """Normalise ``pages`` / ``page_sequence`` / ``page`` into attempt lists.

    ``pages`` / ``page_sequence`` are the direct page sequence and take
    precedence; ``page_attempts`` describes one sequence per retry round.
    A record may also describe its two phases directly with ``first_screen``
    and ``async`` keys, which is expanded into a two-step sequence.
    """
    seq = record.get("pages")
    if seq is None:
        seq = record.get("page_sequence")
    if seq is None:
        seq = record.get("sequence")
    if seq is None and isinstance(record.get("page"), Mapping):
        seq = [record["page"]]
    if seq is None and isinstance(record.get("page"), list):
        seq = record["page"]

    if seq is None:
        phase_steps = _phase_steps(record)
        if phase_steps:
            seq = phase_steps

    if seq is not None:
        if isinstance(seq, Mapping):
            seq = _mapping_to_steps(seq)
        if isinstance(seq, list):
            return [list(seq)]
        return []

    explicit = record.get("page_attempts") or record.get("attempts")
    if isinstance(explicit, list) and explicit and all(isinstance(x, list) for x in explicit):
        return [list(x) for x in explicit]

    if any(key in record for key in ("html", "html_text", "body")):
        seq = [
            {
                "kind": record.get("page_kind") or "first_screen",
                "html": record.get("html") or record.get("body"),
                "text": record.get("text"),
                "listings": record.get("listings"),
                "fields": record.get("fields"),
            }
        ]
        return [seq]
    return []


_PHASE_ORDER = ("first_screen", "first", "initial", "async", "async_response", "xhr", "async_data")


def _phase_steps(record: Mapping[str, Any]) -> list[Any]:
    steps: list[Any] = []
    for phase in _PHASE_ORDER:
        if phase not in record or record[phase] is None:
            continue
        payload = record[phase]
        if isinstance(payload, Mapping):
            step = dict(payload)
            step.setdefault("kind", "async" if phase == "async_data" else phase)
        else:
            step = {"kind": "async" if phase == "async_data" else phase, "data": payload}
        steps.append(step)
    return steps


def _mapping_to_steps(sequence: Mapping[str, Any]) -> list[Any]:
    """Expand ``{"first_screen": ..., "async": ...}`` into ordered steps."""
    known = [key for key in sequence if str(key).lower() in _PHASE_ORDER]
    if not known:
        return [dict(sequence)]
    steps: list[Any] = []
    for key in sorted(known, key=lambda k: _PHASE_ORDER.index(str(k).lower())):
        payload = sequence[key]
        if isinstance(payload, Mapping):
            step = dict(payload)
        else:
            step = {"data": payload}
        step.setdefault("kind", str(key).lower())
        steps.append(step)
    return steps


def _unwrap_phase_step(step: Any) -> Any:
    """Turn ``{"first_screen": {...}}`` into ``{"kind": "first_screen", ...}``."""
    if not isinstance(step, Mapping) or len(step) != 1:
        return step
    (key, value), = step.items()
    if str(key).lower() not in _PHASE_ORDER:
        return step
    if isinstance(value, Mapping):
        inner = dict(value)
        inner.setdefault("kind", str(key).lower())
        return inner
    return {"kind": str(key).lower(), "data": value}


def has_page_payload(record: Mapping[str, Any]) -> bool:
    """True when *record* carries a page or page sequence to render."""
    for key in (
        "pages",
        "page_sequence",
        "sequence",
        "page_attempts",
        "page",
        "html",
        "html_text",
        "body",
        "first_screen",
        "async",
        "async_data",
    ):
        if key in record and record.get(key) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# 单次渲染
# ---------------------------------------------------------------------------

def _render_once(
    steps: list[Any],
    policy: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    method = policy.get("method") or _fetch_policy.METHOD_DYNAMIC
    dynamic = method == _fetch_policy.METHOD_DYNAMIC
    timeout_ms = _as_int(policy.get("timeout_ms"), 0)

    listings: list[dict[str, Any]] = []
    fields: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    waited_ms = 0
    budget_exceeded = False
    async_expected = bool(record.get("expect_async"))
    async_delivered = False
    async_skipped = False
    shell_seen = False
    error_kind: str | None = None
    partial_reason: str | None = None
    blocked = False
    parse_lost = 0

    for raw_step in steps:
        raw_step = _unwrap_phase_step(raw_step)
        kind = _step_kind(raw_step)
        if kind == KIND_ASYNC:
            async_expected = True
            if not dynamic:
                async_skipped = True
                trace.append({"kind": kind, "skipped": "static_method", "listings": 0})
                continue

        wait_ms = _step_wait_ms(raw_step)
        if kind in (KIND_ASYNC, KIND_TIMEOUT) and wait_ms:
            if waited_ms + wait_ms > timeout_ms:
                budget_exceeded = True
                waited_ms = timeout_ms
                error_kind = ERROR_TIMEOUT
                trace.append(
                    {"kind": kind, "wait_ms": wait_ms, "waited_ms": waited_ms,
                     "listings": 0, "error_kind": ERROR_TIMEOUT}
                )
                break
            waited_ms += wait_ms

        if kind == KIND_TIMEOUT:
            error_kind = ERROR_TIMEOUT
            budget_exceeded = True
            trace.append({"kind": kind, "waited_ms": waited_ms, "listings": 0,
                          "error_kind": ERROR_TIMEOUT})
            break

        if kind == KIND_CAPTCHA:
            blocked = True
            error_kind = ERROR_CAPTCHA
            trace.append({"kind": kind, "waited_ms": waited_ms, "listings": 0,
                          "error_kind": ERROR_CAPTCHA})
            break

        if kind == KIND_ERROR:
            text = _lower_join(_text_of(_step_payload(raw_step)),
                               raw_step.get("message") if isinstance(raw_step, Mapping) else None)
            status = _as_int(raw_step.get("status"), 200) if isinstance(raw_step, Mapping) else 200
            if status in (401, 403):
                error_kind = ERROR_BLOCKED
                blocked = True
            elif status == 429:
                error_kind = ERROR_CAPTCHA
                blocked = True
            else:
                error_kind = ERROR_HTTP
            if _looks_captcha(text):
                error_kind = ERROR_CAPTCHA
                blocked = True
            trace.append({"kind": kind, "waited_ms": waited_ms, "listings": 0,
                          "error_kind": error_kind})
            break

        if kind == KIND_EMPTY_SHELL:
            shell_seen = True
            trace.append({"kind": kind, "waited_ms": waited_ms, "listings": 0})
            continue

        payload = _step_payload(raw_step)
        step_listings = _listings_from_payload(payload)
        raw_listings = payload.get("listings") if isinstance(payload, Mapping) else None
        if isinstance(raw_listings, list):
            parse_lost += sum(1 for item in raw_listings if isinstance(item, Mapping) and listing_key(item) is None)

        step_fields = {}
        if isinstance(raw_step, Mapping) and isinstance(raw_step.get("fields"), Mapping):
            step_fields.update(raw_step["fields"])
        if isinstance(payload, Mapping) and isinstance(payload.get("fields"), Mapping):
            step_fields.update(payload["fields"])

        if kind == KIND_ASYNC:
            if step_listings:
                async_delivered = True
            else:
                # 异步接口返回了空内容/验证码/错误结构，不能当作成功。
                body = _lower_join(_text_of(payload),
                                   raw_step.get("message") if isinstance(raw_step, Mapping) else None)
                status = _as_int(raw_step.get("status"), 200) if isinstance(raw_step, Mapping) else 200
                if _looks_captcha(body) or status == 429:
                    blocked = True
                    error_kind = ERROR_CAPTCHA
                elif status in (401, 403):
                    blocked = True
                    error_kind = ERROR_BLOCKED
                elif status >= 400 or _looks_error(body):
                    error_kind = ERROR_HTTP
                trace.append(
                    {"kind": kind, "waited_ms": waited_ms, "listings": 0,
                     "error_kind": error_kind}
                )
                if error_kind in (ERROR_CAPTCHA, ERROR_BLOCKED, ERROR_HTTP):
                    break
                partial_reason = partial_reason or PARTIAL_ASYNC
                continue
        listings.extend(step_listings)
        fields.update(step_fields)
        trace.append(
            {
                "kind": kind,
                "waited_ms": waited_ms,
                "listings": len(step_listings),
                "error_kind": None,
            }
        )

    listings = _dedupe(listings)
    if parse_lost and listings and partial_reason is None:
        partial_reason = PARTIAL_PARSE

    if isinstance(record.get("fields"), Mapping):
        merged = dict(record["fields"])
        merged.update(fields)
        fields = merged

    required = policy.get("required_fields") or record.get("required_fields") or []
    missing_fields: list[str] = []
    for field in required:
        name = str(field)
        if name in fields:
            continue
        if listings and all(name in item for item in listings):
            continue
        missing_fields.append(name)
    if missing_fields and listings and partial_reason is None:
        partial_reason = PARTIAL_FIELDS
    if not listings and record.get("expected_fields"):
        for field in record["expected_fields"]:
            if str(field) not in fields:
                missing_fields.append(str(field))

    # ---- 完成状态判定：只有真正抽到房源才算有效页面 ----
    if listings:
        partial = bool(
            error_kind
            or partial_reason
            or missing_fields
            or (async_expected and not async_delivered and dynamic)
        )
        status = STATUS_PARTIAL if partial else STATUS_COMPLETE
        if partial and error_kind:
            page_kind = _kind_for_error(error_kind)
            if partial_reason is None:
                partial_reason = PARTIAL_BLOCKED if blocked else PARTIAL_ASYNC
        elif partial and (async_expected and not async_delivered):
            page_kind = KIND_FIRST_SCREEN
            partial_reason = partial_reason or PARTIAL_ASYNC
        elif partial_reason == PARTIAL_FIELDS:
            page_kind = KIND_ASYNC if async_delivered else KIND_FIRST_SCREEN
        else:
            page_kind = KIND_ASYNC if async_delivered else KIND_FIRST_SCREEN
        source = KIND_FIRST_SCREEN if not async_delivered else KIND_ASYNC
    else:
        status = STATUS_FAILED
        partial = False
        source = None
        partial_reason = None
        if error_kind is None:
            if shell_seen or budget_exceeded:
                error_kind = ERROR_EMPTY_SHELL if not budget_exceeded else ERROR_TIMEOUT
            elif parse_lost:
                error_kind = ERROR_PARSE
            else:
                error_kind = ERROR_EMPTY_SHELL
        page_kind = _kind_for_error(error_kind)

    if async_skipped and status == STATUS_COMPLETE:
        # 静态抓取跳过了异步内容：若页面声明了异步数据，首屏结果只能算局部。
        status = STATUS_PARTIAL
        partial = True
        partial_reason = PARTIAL_ASYNC
        page_kind = KIND_FIRST_SCREEN

    return {
        "status": status,
        "complete": status == STATUS_COMPLETE,
        "partial": status == STATUS_PARTIAL,
        "valid": bool(listings),
        "page_kind": page_kind,
        "source": source,
        "error_kind": error_kind,
        "partial_reason": partial_reason,
        "listings": listings,
        "fields": fields,
        "missing_fields": _unique(missing_fields),
        "required_fields": [str(x) for x in required],
        "attempts": 1,
        "timeout_ms": timeout_ms,
        "waited_ms": waited_ms,
        "budget_exceeded": budget_exceeded,
        "blocked": blocked,
        "async_expected": async_expected,
        "async_delivered": async_delivered,
        "async_skipped": async_skipped,
        "steps": trace,
        "method": method,
    }


def _kind_for_error(error_kind: str | None) -> str:
    if error_kind == ERROR_CAPTCHA:
        return KIND_CAPTCHA
    if error_kind == ERROR_TIMEOUT:
        return KIND_TIMEOUT
    if error_kind == ERROR_EMPTY_SHELL:
        return KIND_EMPTY_SHELL
    if error_kind in (ERROR_HTTP, ERROR_BLOCKED):
        return KIND_ERROR
    if error_kind == ERROR_PARSE:
        return KIND_ERROR
    if error_kind == ERROR_NO_LISTINGS:
        return KIND_EMPTY_SHELL
    return KIND_ERROR


def _unique(values: Iterable[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def _cache_settings(record: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    config = record.get("cache") if isinstance(record.get("cache"), Mapping) else {}
    ttl = config.get("ttl_ms", policy.get("cache_ttl_ms", 0))
    key = config.get("key") or record.get("url") or record.get("id")
    # 缓存是显式能力：只有调用方声明 use_cache / cache.enabled 才启用，
    # 避免同一批离线记录互相命中上一轮的缓存。
    enabled = config.get("enabled", record.get("use_cache", False))
    if config.get("valid") is False:
        invalidated = True
    else:
        invalidated = False
    return {
        "key": key,
        "ttl_ms": _as_int(ttl, 0),
        "enabled": bool(enabled) and key is not None and _as_int(ttl, 0) > 0,
        "invalidated": invalidated,
        "store": config.get("store"),
    }


def _cache_store_for(
    record: Mapping[str, Any], settings: Mapping[str, Any], override: Any
) -> Any:
    if override is not None:
        return override
    candidate = record.get("cache_store")
    if isinstance(candidate, dict):
        return candidate
    if isinstance(settings.get("store"), dict):
        return settings["store"]
    return _DEFAULT_CACHE


def _cache_lookup(
    store: dict[str, Any], key: str, ttl_ms: int, now: int
) -> tuple[dict[str, Any] | None, int | None, bool]:
    """Return ``(fresh_entry, stale_age_ms, had_stale)``."""
    entry = store.get(key) if isinstance(store, Mapping) else None
    if not isinstance(entry, Mapping):
        return None, None, False
    stored_at = _as_int(entry.get("stored_at_ms"), now)
    age = max(0, now - stored_at)
    if age >= ttl_ms:
        return None, age, True
    if entry.get("valid") is False:
        return None, age, True
    return dict(entry), age, False


def _cached_extraction(entry: Mapping[str, Any], state: str, age: int, ttl: int) -> dict[str, Any]:
    cached = entry.get("extraction")
    if not isinstance(cached, Mapping):
        return {}
    extraction = copy.deepcopy(dict(cached))
    extraction.update(
        {
            "attempts": 0,
            "waited_ms": 0,
            "budget_exceeded": False,
            "cache_state": state,
            "cache_age_ms": age,
            "cache_ttl_ms": ttl,
            "source": "cache",
            "recovered": state == CACHE_STALE,
        }
    )
    extraction["complete"] = extraction.get("status") == STATUS_COMPLETE
    extraction["partial"] = extraction.get("status") == STATUS_PARTIAL
    extraction["valid"] = bool(extraction.get("listings"))
    extraction.setdefault("steps", [])
    return extraction


def _store_cache(
    store: dict[str, Any], key: str, extraction: Mapping[str, Any], now: int
) -> None:
    store[key] = {
        "stored_at_ms": now,
        "valid": True,
        "status": extraction.get("status"),
        "extraction": copy.deepcopy(dict(extraction)),
    }


def reset_cache() -> None:
    """Clear the module level cache (tests / long running workers)."""
    _DEFAULT_CACHE.clear()


def clear_cache(key: str | None = None) -> None:
    if key is None:
        _DEFAULT_CACHE.clear()
    else:
        _DEFAULT_CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# 对外渲染接口
# ---------------------------------------------------------------------------

def render_page(
    record: Mapping[str, Any],
    *,
    cache: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Render *record*'s page sequence and return the extraction result.

    The returned mapping is JSON-friendly and always carries ``status``
    (``complete`` / ``partial`` / ``failed``), ``page_kind``, ``error_kind``,
    ``listings`` and the wait/cache boundary fields.
    """
    policy = _fetch_policy.resolve(record)
    if now_ms is None:
        now_ms = record.get("now_ms")
    # 缓存用墙上时钟，保证跨进程"重启"后 TTL 判断仍然正确（monotonic 会重置）。
    now = _as_int(now_ms, int(time.time() * 1000))

    settings = _cache_settings(record, policy)
    store = _cache_store_for(record, settings, cache)

    if not has_page_payload(record):
        return {
            "status": STATUS_FAILED,
            "complete": False,
            "partial": False,
            "valid": False,
            "page_kind": KIND_ERROR,
            "source": None,
            "error_kind": ERROR_NO_LISTINGS,
            "partial_reason": None,
            "listings": [],
            "fields": {},
            "missing_fields": [],
            "required_fields": [],
            "attempts": 0,
            "timeout_ms": _as_int(policy.get("timeout_ms"), 0),
            "waited_ms": 0,
            "budget_exceeded": False,
            "blocked": False,
            "steps": [],
            "method": policy.get("method"),
            "cache_state": CACHE_DISABLED,
            "cache_key": settings.get("key"),
            "cache_age_ms": None,
            "cache_ttl_ms": settings.get("ttl_ms"),
        }

    cache_state = CACHE_DISABLED
    cache_age: int | None = None
    stale_entry: dict[str, Any] | None = None

    if settings["enabled"] and not settings["invalidated"]:
        fresh, age, had_stale = _cache_lookup(store, settings["key"], settings["ttl_ms"], now)
        cache_age = age
        if fresh is not None:
            hit = _cached_extraction(fresh, CACHE_HIT, age or 0, settings["ttl_ms"])
            hit["cache_key"] = settings.get("key")
            hit["cache_ttl_ms"] = settings.get("ttl_ms")
            return hit
        if had_stale:
            cache_state = CACHE_MISS
            stale_entry = store.get(settings["key"]) if isinstance(store, Mapping) else None
        else:
            cache_state = CACHE_MISS
    elif settings["enabled"]:
        # 显式失效：忽略旧条目，仅保留其内容用于可选的兜底恢复。
        cache_state = CACHE_INVALIDATED
        stale_entry = store.get(settings["key"]) if isinstance(store, Mapping) else None

    attempts = _page_attempts(record)
    retries = _as_int(policy.get("retries"), 0)
    on_partial = policy.get("on_partial", _fetch_policy.ON_PARTIAL_KEEP)
    result: dict[str, Any] | None = None
    best_partial: dict[str, Any] | None = None

    for attempt in range(retries + 1):
        steps = attempts[min(attempt, len(attempts) - 1)] if attempts else []
        current = _render_once(steps, policy, record)
        current["attempts"] = attempt + 1
        current["retries"] = retries

        if current["status"] == STATUS_PARTIAL:
            if best_partial is None or len(current["listings"]) > len(best_partial["listings"]):
                best_partial = current
            if on_partial != _fetch_policy.ON_PARTIAL_RETRY:
                result = current
                break
        elif current["status"] == STATUS_COMPLETE:
            result = current
            break
        else:  # failed
            result = current
            if attempt >= retries:
                break

    if result is None:
        result = best_partial or _render_once([], policy, record)
        result["attempts"] = retries + 1

    # 整页重试仍失败/仍不完整时，保留此前可用的部分字段，避免"越重试越少"。
    if (
        result["status"] == STATUS_FAILED
        and best_partial is not None
        and on_partial == _fetch_policy.ON_PARTIAL_RETRY
    ):
        best_partial["retried"] = True
        best_partial["retry_failed"] = True
        result = best_partial
    elif (
        result["status"] == STATUS_PARTIAL
        and result.get("attempts", 1) > 1
        and on_partial == _fetch_policy.ON_PARTIAL_RETRY
    ):
        result["retried"] = True
        result["retry_failed"] = True

    result.setdefault("retried", result.get("attempts", 1) > 1)
    result["cache_key"] = settings.get("key")
    result["cache_ttl_ms"] = settings.get("ttl_ms")

    # ---- 缓存写回 / 失效 / 恢复 ----
    if settings["enabled"]:
        if result["status"] == STATUS_COMPLETE:
            refreshed = bool(stale_entry) or settings["invalidated"]
            _store_cache(store, settings["key"], result, now)
            result["cache_state"] = CACHE_REFRESHED if refreshed else CACHE_STORED
            result["cache_age_ms"] = 0
        else:
            # 失败/局部页面不写入缓存，并让旧条目失效，避免错误页被当作有效挂牌。
            if settings["key"] in store:
                del store[settings["key"]]
            result["cache_state"] = CACHE_INVALIDATED if stale_entry is not None else cache_state
            result["cache_age_ms"] = cache_age
    else:
        result["cache_state"] = CACHE_DISABLED

    # 缓存兜底：仅在显式开启 cache_fallback 时使用过期但仍 valid 的完整缓存。
    if (
        result["status"] == STATUS_FAILED
        and policy.get("cache_fallback")
        and isinstance(stale_entry, Mapping)
        and isinstance(stale_entry.get("extraction"), Mapping)
        and (stale_entry["extraction"].get("status") == STATUS_COMPLETE)
        and not settings["invalidated"]
    ):
        recovered = _cached_extraction(
            stale_entry, CACHE_STALE, cache_age or 0, settings["ttl_ms"]
        )
        recovered["recovered"] = True
        recovered["failed_status"] = STATUS_FAILED
        recovered["failed_error_kind"] = result.get("error_kind")
        recovered["attempts"] = result.get("attempts", 0)
        recovered["retried"] = result.get("retried", False)
        return recovered

    return result


def render(record: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`render_page` (kept short for callers)."""
    return render_page(record, **kwargs)


def process(record: Mapping[str, Any]) -> dict[str, Any]:
    """Pipeline stage: annotate *record* with the extraction result.

    Records without page payload keep the historical naive behaviour so the
    original happy-path (and its successful extraction interface) still works.
    """
    rec = dict(record)
    rec.setdefault("_stage_renderer", "naive")
    if not has_page_payload(rec):
        return rec

    cache = rec.get("cache_store") if isinstance(rec.get("cache_store"), dict) else None
    extraction = render_page(rec, cache=cache)
    rec["extraction"] = extraction
    rec["render_status"] = extraction["status"]
    rec["page_kind"] = extraction["page_kind"]
    rec["error_kind"] = extraction["error_kind"]
    rec["render_complete"] = extraction["complete"]
    rec["render_partial"] = extraction["partial"]
    return rec


__all__ = [
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_FAILED",
    "STATUSES",
    "KIND_FIRST_SCREEN",
    "KIND_ASYNC",
    "KIND_EMPTY_SHELL",
    "KIND_CAPTCHA",
    "KIND_TIMEOUT",
    "KIND_ERROR",
    "PAGE_KINDS",
    "ERROR_EMPTY_SHELL",
    "ERROR_CAPTCHA",
    "ERROR_TIMEOUT",
    "ERROR_HTTP",
    "ERROR_BLOCKED",
    "ERROR_PARSE",
    "ERROR_NO_LISTINGS",
    "PARTIAL_ASYNC",
    "PARTIAL_FIELDS",
    "PARTIAL_PARSE",
    "PARTIAL_BLOCKED",
    "CACHE_DISABLED",
    "CACHE_MISS",
    "CACHE_HIT",
    "CACHE_STALE",
    "CACHE_INVALIDATED",
    "CACHE_STORED",
    "CACHE_REFRESHED",
    "classify_page",
    "has_page_payload",
    "listing_key",
    "render_page",
    "render",
    "process",
    "reset_cache",
    "clear_cache",
]
