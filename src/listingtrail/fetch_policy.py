"""单一职责：决定一份页面/记录应采用哪种抓取方式，并给出等待与缓存边界。

本模块只回答"怎么抓"：

* ``static`` —— 直接请求 HTML，首屏即内容；
* ``dynamic`` —— 需要浏览器渲染、等待异步接口/水合完成后再抽取。

不负责解析页面，也不负责判断页面是否抓取成功（那是 ``renderer`` 的职责）。
策略同时定义等待预算、重试次数、缓存 TTL 和"局部成功"时的默认动作，
让下游（renderer / snapshot）能够引用同一份边界配置。
"""
from __future__ import annotations

from typing import Any, Mapping

METHOD_STATIC = "static"
METHOD_DYNAMIC = "dynamic"
METHODS = (METHOD_STATIC, METHOD_DYNAMIC)

# 局部成功：保留已抽取到的可用字段，同时允许调用方决定是否补抓。
ON_PARTIAL_KEEP = "keep"
ON_PARTIAL_RETRY = "retry"
ON_PARTIAL_ACTIONS = (ON_PARTIAL_KEEP, ON_PARTIAL_RETRY)

DEFAULT_TIMEOUT_MS = 2_000
DEFAULT_RETRIES = 1
DEFAULT_CACHE_TTL_MS = 60_000
DEFAULT_ON_PARTIAL = ON_PARTIAL_KEEP

DEFAULT_POLICY: dict[str, Any] = {
    "method": METHOD_DYNAMIC,
    "wait_for": "async",          # first_screen | async | networkidle
    "timeout_ms": DEFAULT_TIMEOUT_MS,
    "retries": DEFAULT_RETRIES,
    "cache_ttl_ms": DEFAULT_CACHE_TTL_MS,
    "on_partial": DEFAULT_ON_PARTIAL,
    "scroll": False,
}

_DYNAMIC_MARKERS = (
    "__next_data__",
    "data-hydrate",
    "data-async",
    "application/json",
    "window.__",
    "react",
    "vue",
    "svelte",
    "nuxt",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "dynamic", "render", "js"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _normalise_method(value: Any) -> str:
    if value is None:
        return METHOD_DYNAMIC
    text = str(value).strip().lower()
    if text in {"static", "http", "html", "plain", "none"}:
        return METHOD_STATIC
    if text in {"dynamic", "browser", "render", "js", "spa", "playwright", "selenium"}:
        return METHOD_DYNAMIC
    return METHOD_DYNAMIC if text else METHOD_DYNAMIC


def _looks_dynamic(record: Mapping[str, Any]) -> bool:
    if _truthy(record.get("requires_js")) or _truthy(record.get("javascript")):
        return True
    if record.get("async") is True or record.get("async_pending") is True:
        return True
    page_type = str(record.get("page_type") or record.get("kind") or "").lower()
    if page_type in {"first_screen", "async", "async_success", "async_pending", "spa"}:
        return True
    html = record.get("html")
    if isinstance(html, str):
        lowered = html.lower()
        if any(marker in lowered for marker in _DYNAMIC_MARKERS):
            return True
    # 带页面序列的记录默认按动态页面处理；只有显式声明静态时才跳过异步阶段。
    for key in (
        "pages",
        "page_sequence",
        "sequence",
        "page_attempts",
        "page",
        "first_screen",
        "async",
    ):
        if key in record and record.get(key):
            return True
    return False


def method_for(record: Mapping[str, Any]) -> str:
    """Return the effective fetch method for *record*.

    Explicit hints win over heuristics.  A record that carries a page payload
    with JS/async markers is treated as ``dynamic`` even when the caller did
    not supply a policy, which matches the real-world "200 but not yet
    hydrated" problem this pipeline has to handle.
    """
    for key in ("fetch_method", "render_mode"):
        if record.get(key):
            value = record[key]
            if isinstance(value, Mapping):
                continue
            return _normalise_method(value)

    raw_policy = record.get("fetch_policy")
    if isinstance(raw_policy, str) and raw_policy.strip():
        return _normalise_method(raw_policy)
    if isinstance(raw_policy, Mapping) and raw_policy.get("method"):
        return _normalise_method(raw_policy["method"])

    if _looks_dynamic(record):
        return METHOD_DYNAMIC
    return METHOD_STATIC


def normalize_policy(policy: Any = None, **overrides: Any) -> dict[str, Any]:
    """Return a complete, JSON-friendly policy dictionary.

    Accepts a method string, a partial mapping, or ``None``.  Both
    ``timeout_s``/``timeout`` and ``timeout_ms`` are supported; the canonical
    field is ``timeout_ms`` so the renderer/snapshot stages share one boundary.
    """
    result = dict(DEFAULT_POLICY)
    provided: dict[str, Any] = {}

    if isinstance(policy, str):
        provided["method"] = policy
    elif isinstance(policy, Mapping):
        provided.update(policy)
    provided.update(overrides)
    result.update(provided)

    result["method"] = _normalise_method(result.get("method"))

    # Canonical wait budget in milliseconds.  ``provided`` distinguishes an
    # explicit ``timeout_s`` from the default ``timeout_ms`` in the template.
    if "timeout_ms" in provided:
        result["timeout_ms"] = _as_int(provided["timeout_ms"], DEFAULT_TIMEOUT_MS)
    elif "timeout_s" in provided:
        result["timeout_ms"] = _as_int(provided["timeout_s"], 0) * 1000
    elif "timeout" in provided:
        result["timeout_ms"] = _as_int(provided["timeout"], 0) * 1000
    else:
        result["timeout_ms"] = _as_int(result.get("timeout_ms"), DEFAULT_TIMEOUT_MS)

    result["retries"] = _as_int(result.get("retries"), DEFAULT_RETRIES)
    result["cache_ttl_ms"] = _as_int(result.get("cache_ttl_ms"), DEFAULT_CACHE_TTL_MS)

    on_partial = str(result.get("on_partial") or DEFAULT_ON_PARTIAL).strip().lower()
    result["on_partial"] = on_partial if on_partial in ON_PARTIAL_ACTIONS else DEFAULT_ON_PARTIAL
    result["scroll"] = _truthy(result.get("scroll"))
    result["wait_for"] = str(result.get("wait_for") or "async").strip().lower()

    # Remove non-canonical duplicates while keeping any extra caller metadata.
    result.pop("timeout_s", None)
    result.pop("timeout", None)
    return result


def resolve(record: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the policy for *record*, merging record/page level settings.

    Precedence for the method: explicit ``overrides`` > page/record
    ``fetch_policy`` > ``fetch_method``/``render_mode`` hints > heuristics.
    """
    policy = normalize_policy(record.get("fetch_policy"))
    policy_method_explicit = _policy_supplies_method(record.get("fetch_policy"))
    if isinstance(record.get("page"), Mapping) and record["page"].get("fetch_policy"):
        policy = normalize_policy(record["page"].get("fetch_policy"))
        policy_method_explicit = _policy_supplies_method(record["page"].get("fetch_policy"))
    if isinstance(record.get("policy"), Mapping):
        policy = normalize_policy(policy, **record["policy"])
        if record["policy"].get("method"):
            policy_method_explicit = True
    if overrides:
        policy = normalize_policy(policy, **overrides)
        if overrides.get("method"):
            policy_method_explicit = True
    if not policy_method_explicit:
        policy["method"] = method_for(record)
    return policy


def _policy_supplies_method(raw_policy: Any) -> bool:
    if isinstance(raw_policy, str) and raw_policy.strip():
        return True
    if isinstance(raw_policy, Mapping) and raw_policy.get("method"):
        return True
    return False


def process(record: Mapping[str, Any]) -> dict[str, Any]:
    """Pipeline stage: annotate a record with its resolved fetch policy.

    The historical marker ``_stage_fetch_policy == "ok"`` is preserved so the
    existing happy-path contract keeps working.
    """
    rec = dict(record)
    policy = resolve(rec)
    rec.setdefault("_stage_fetch_policy", "ok")
    rec["fetch_policy"] = policy
    rec["fetch_method"] = policy["method"]
    return rec


__all__ = [
    "METHOD_STATIC",
    "METHOD_DYNAMIC",
    "METHODS",
    "ON_PARTIAL_KEEP",
    "ON_PARTIAL_RETRY",
    "DEFAULT_POLICY",
    "method_for",
    "normalize_policy",
    "resolve",
    "process",
]
