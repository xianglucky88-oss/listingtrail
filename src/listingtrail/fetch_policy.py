"""单一职责：按来源（中介站）表达采集决策。

本模块只做“这次该怎样请求、结果属于哪一类、下一步怎么办”的判断，不解析页面、
不维护挂牌状态、不写业务库；它产出的 ``FetchDecision`` 会由
``renderer → statusline → snapshot → store`` 继续传递和落库。

来源级策略显式覆盖：
* 请求方式（普通 HTTP / 渲染）、条件请求（ETag / Last-Modified）
* 重试次数、退避与可重试的失败类别
* 限速间隔与最大等待预算
* 熔断阈值、冷却时间、熔断期是否允许半开探测
* 普通请求被拦截后是否升级到渲染

失败语义严格区分：
``ok`` / ``not_modified`` / ``empty_result``（合法空结果）/
``blocked``（被拦截）/ ``temporarily_unavailable``（暂时不可用）/
``delisted``（来源确认下架）。只有最后一种可以成为“下架事实”。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

# ---------------------------------------------------------------------------
# 结果与失败类别（稳定字符串，落库/追踪使用）
# ---------------------------------------------------------------------------

OUTCOME_OK = "ok"
OUTCOME_NOT_MODIFIED = "not_modified"
OUTCOME_EMPTY = "empty_result"
OUTCOME_BLOCKED = "blocked"
OUTCOME_TEMPORARY = "temporarily_unavailable"
OUTCOME_DELISTED = "delisted"

OUTCOMES = frozenset(
    {
        OUTCOME_OK,
        OUTCOME_NOT_MODIFIED,
        OUTCOME_EMPTY,
        OUTCOME_BLOCKED,
        OUTCOME_TEMPORARY,
        OUTCOME_DELISTED,
    }
)

#: 代表“来源没给出可信观测”的结果；下游不得据此改写已有挂牌状态。
DEGRADED_OUTCOMES = frozenset({OUTCOME_BLOCKED, OUTCOME_TEMPORARY})

#: 代表“本次确实看到了来源内容”的结果。
OBSERVED_OUTCOMES = frozenset(
    {OUTCOME_OK, OUTCOME_NOT_MODIFIED, OUTCOME_EMPTY, OUTCOME_DELISTED}
)

CATEGORY_ACCESS_DENIED = "access_denied"
CATEGORY_CHALLENGE = "challenge"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_SERVER_ERROR = "server_error"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_NETWORK = "network"
CATEGORY_CIRCUIT_OPEN = "circuit_open"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_UNKNOWN = "unknown"

FAILURE_CATEGORIES = frozenset(
    {
        CATEGORY_ACCESS_DENIED,
        CATEGORY_CHALLENGE,
        CATEGORY_RATE_LIMITED,
        CATEGORY_SERVER_ERROR,
        CATEGORY_TIMEOUT,
        CATEGORY_NETWORK,
        CATEGORY_CIRCUIT_OPEN,
        CATEGORY_NOT_FOUND,
        CATEGORY_UNKNOWN,
    }
)

BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# 策略对象（不可变；版本由内容确定性派生）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 1.0
    retry_categories: tuple[str, ...] = (
        CATEGORY_TIMEOUT,
        CATEGORY_NETWORK,
        CATEGORY_SERVER_ERROR,
        CATEGORY_RATE_LIMITED,
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "retry_categories": list(self.retry_categories),
        }


@dataclass(frozen=True)
class RateLimitPolicy:
    min_interval_seconds: float = 0.0
    max_wait_seconds: float = 30.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_interval_seconds": self.min_interval_seconds,
            "max_wait_seconds": self.max_wait_seconds,
        }


@dataclass(frozen=True)
class BreakerPolicy:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    allow_probe: bool = True
    probe_after_seconds: float | None = None

    def probe_delay(self) -> float:
        if self.probe_after_seconds is None:
            return self.cooldown_seconds
        return max(self.cooldown_seconds, self.probe_after_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "allow_probe": self.allow_probe,
            "probe_after_seconds": self.probe_delay(),
        }


@dataclass(frozen=True)
class SourcePolicy:
    """单个来源的采集规则。"""

    source: str = "default"
    request_mode: str = "plain"  # plain | render
    conditional_requests: bool = True
    render_on_block: bool = True
    #: 拦截后保持渲染模式的记录数；None 表示本会话内一直渲染。
    render_hold_records: int | None = None
    treat_404_as_delisted: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    rate_limit: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    circuit_breaker: BreakerPolicy = field(default_factory=BreakerPolicy)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "request_mode": self.request_mode,
            "conditional_requests": self.conditional_requests,
            "render_on_block": self.render_on_block,
            "render_hold_records": self.render_hold_records,
            "treat_404_as_delisted": self.treat_404_as_delisted,
            "retry": self.retry.as_dict(),
            "rate_limit": self.rate_limit.as_dict(),
            "circuit_breaker": self.circuit_breaker.as_dict(),
        }

    def fingerprint(self) -> str:
        return _digest(self.as_dict())


@dataclass(frozen=True)
class PolicySet:
    """一组来源策略 + 确定性版本号。"""

    policies: Mapping[str, SourcePolicy]
    default: SourcePolicy
    version: str

    def for_source(self, source: str) -> SourcePolicy:
        name = normalize_source(source)
        policy = self.policies.get(name)
        if policy is None:
            policy = replace(self.default, source=name)
        return policy

    def fingerprint_for(self, source: str) -> str:
        return self.for_source(source).fingerprint()

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "default": self.default.as_dict(),
            "policies": {
                name: policy.as_dict() for name, policy in sorted(self.policies.items())
            },
        }


def normalize_source(source: Any) -> str:
    return str(source or "").strip().lower()


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


DEFAULT_CONFIG: dict[str, Any] = {
    "request_mode": "plain",
    "conditional_requests": True,
    "render_on_block": True,
    "render_hold_records": None,
    "treat_404_as_delisted": True,
    "retry": {
        "max_attempts": 3,
        "backoff_seconds": 1.0,
        "retry_categories": [
            CATEGORY_TIMEOUT,
            CATEGORY_NETWORK,
            CATEGORY_SERVER_ERROR,
            CATEGORY_RATE_LIMITED,
        ],
    },
    "rate_limit": {"min_interval_seconds": 0.0, "max_wait_seconds": 30.0},
    "circuit_breaker": {
        "failure_threshold": 3,
        "cooldown_seconds": 60.0,
        "allow_probe": True,
        "probe_after_seconds": 60.0,
    },
}

#: 已知中介站的差异化默认值。真实部署可由配置文件覆盖。
DEFAULT_SOURCE_CONFIGS: dict[str, dict[str, Any]] = {
    "alpha": {
        "request_mode": "plain",
        "rate_limit": {"min_interval_seconds": 2.0, "max_wait_seconds": 10.0},
        "circuit_breaker": {
            "failure_threshold": 2,
            "cooldown_seconds": 30.0,
            "allow_probe": True,
            "probe_after_seconds": 30.0,
        },
    },
    "beta": {
        "request_mode": "plain",
        "rate_limit": {"min_interval_seconds": 0.0, "max_wait_seconds": 30.0},
        "circuit_breaker": {
            "failure_threshold": 3,
            "cooldown_seconds": 60.0,
            "allow_probe": True,
            "probe_after_seconds": 60.0,
        },
    },
}


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in base.items()}
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build_policy(source: str, config: Mapping[str, Any]) -> SourcePolicy:
    retry_cfg = dict(config.get("retry") or {})
    if "retry_categories" in retry_cfg:
        retry_cfg["retry_categories"] = tuple(retry_cfg["retry_categories"])
    hold = config.get("render_hold_records")
    return SourcePolicy(
        source=normalize_source(source),
        request_mode=str(config.get("request_mode", "plain")),
        conditional_requests=bool(config.get("conditional_requests", True)),
        render_on_block=bool(config.get("render_on_block", True)),
        render_hold_records=(int(hold) if hold is not None else None),
        treat_404_as_delisted=bool(config.get("treat_404_as_delisted", True)),
        retry=RetryPolicy(**retry_cfg),
        rate_limit=RateLimitPolicy(**(config.get("rate_limit") or {})),
        circuit_breaker=BreakerPolicy(**(config.get("circuit_breaker") or {})),
    )


def load_policies(overrides: Mapping[str, Any] | None = None) -> PolicySet:
    """构造策略集；``overrides`` 以来源名为键做深合并。

    版本号是策略内容的确定性摘要：配置更新后版本必然变化，可据此在批次
    检查点与请求轨迹里识别“配置变更时批次尚未完成”。
    """
    configs: dict[str, dict[str, Any]] = {
        name: _deep_merge(DEFAULT_CONFIG, cfg)
        for name, cfg in DEFAULT_SOURCE_CONFIGS.items()
    }
    default_config = dict(DEFAULT_CONFIG)

    for name, patch in (overrides or {}).items():
        if isinstance(patch, SourcePolicy):
            configs[normalize_source(name)] = patch.as_dict()
            continue
        base = configs.get(normalize_source(name), default_config)
        configs[normalize_source(name)] = _deep_merge(base, dict(patch))

    policies = {
        name: _build_policy(name, cfg) for name, cfg in sorted(configs.items())
    }
    default_policy = _build_policy("default", default_config)
    version = _digest(
        {
            "default": default_policy.as_dict(),
            "policies": {n: p.as_dict() for n, p in policies.items()},
        }
    )
    return PolicySet(policies=policies, default=default_policy, version=version)


_DEFAULT_POLICY_SET: PolicySet | None = None


def default_policies() -> PolicySet:
    global _DEFAULT_POLICY_SET
    if _DEFAULT_POLICY_SET is None:
        _DEFAULT_POLICY_SET = load_policies()
    return _DEFAULT_POLICY_SET


# ---------------------------------------------------------------------------
# 观测读取：把来源回执里的各种别名归一成一份 observation
# ---------------------------------------------------------------------------

_OBS_ALIASES: dict[str, tuple[str, ...]] = {
    "http_status": ("http_status", "status_code", "code", "http_code"),
    "error": ("error", "fetch_error", "exception", "error_type"),
    "blocked": ("blocked", "is_blocked", "access_denied"),
    "challenge": ("challenge", "captcha", "login_required", "robot_check"),
    "empty": ("empty", "empty_result", "no_results"),
    "delisted": ("delisted", "removed", "withdrawn", "gone", "off_market"),
    "etag": ("etag", "ETag"),
    "last_modified": ("last_modified", "lastModified", "modified"),
    "retry_after": ("retry_after", "retry_after_seconds"),
    "render_required": ("render_required", "needs_render", "js_required"),
    "items": ("items", "results", "listings"),
    "outcome": ("outcome",),
    "failure_category": ("failure_category", "failure"),
    "attempts": ("attempts", "attempt"),
}


def read_observation(record: Mapping[str, Any]) -> dict[str, Any]:
    """从记录的 ``fetch`` 子对象/顶层字段读取一份来源回执。

    嵌套 ``fetch`` 优先于顶层别名；未出现的键不会凭空补值。
    """
    nested = record.get("fetch")
    sources: list[Mapping[str, Any]] = []
    if isinstance(nested, Mapping):
        sources.append(nested)
    sources.append(record)

    obs: dict[str, Any] = {}
    for canonical, aliases in _OBS_ALIASES.items():
        for alias in aliases:
            found = None
            for source in sources:
                if alias in source:
                    found = source[alias]
                    break
            if found is not None:
                obs[canonical] = found
                break

    status = obs.get("http_status")
    if status is not None:
        try:
            obs["http_status"] = int(status)
        except (TypeError, ValueError):
            obs.pop("http_status", None)
    return obs


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def classify_observation(
    obs: Mapping[str, Any], policy: SourcePolicy
) -> tuple[str, str | None, str]:
    """把回执归类为 (outcome, failure_category, reason)。

    被拦截优先于空结果：拦截页往往也是“零条”，绝不能当成合法空结果。
    """
    explicit = obs.get("outcome")
    if isinstance(explicit, str) and explicit in OUTCOMES:
        category = obs.get("failure_category")
        return explicit, (str(category) if category else None), "explicit_outcome"

    status = obs.get("http_status")
    if _bool(obs.get("blocked")):
        return OUTCOME_BLOCKED, CATEGORY_ACCESS_DENIED, "blocked_signal"
    if _bool(obs.get("challenge")):
        return OUTCOME_BLOCKED, CATEGORY_CHALLENGE, "challenge_detected"
    if status in (401, 403, 407, 451):
        return OUTCOME_BLOCKED, CATEGORY_ACCESS_DENIED, f"http_{status}"
    if status == 429:
        return OUTCOME_TEMPORARY, CATEGORY_RATE_LIMITED, "http_429"
    if status == 304:
        return OUTCOME_NOT_MODIFIED, None, "http_304"
    if status in (404, 410):
        if policy.treat_404_as_delisted:
            return OUTCOME_DELISTED, CATEGORY_NOT_FOUND, f"http_{status}"
        return OUTCOME_TEMPORARY, CATEGORY_NOT_FOUND, f"http_{status}_unconfirmed"
    if status is not None and 500 <= status < 600:
        return OUTCOME_TEMPORARY, CATEGORY_SERVER_ERROR, f"http_{status}"

    error = str(obs.get("error") or "").strip().lower()
    if error:
        if any(k in error for k in ("timeout", "timed out", "deadline")):
            return OUTCOME_TEMPORARY, CATEGORY_TIMEOUT, "timeout"
        if any(k in error for k in ("dns", "connection", "connect", "reset", "refused", "ssl")):
            return OUTCOME_TEMPORARY, CATEGORY_NETWORK, "network_error"
        if any(k in error for k in ("blocked", "forbidden", "captcha", "challenge")):
            return OUTCOME_BLOCKED, CATEGORY_ACCESS_DENIED, "blocked_error"

    if _bool(obs.get("delisted")):
        return OUTCOME_DELISTED, None, "removed_signal"

    if _bool(obs.get("empty")):
        return OUTCOME_EMPTY, None, "empty_result"
    items = obs.get("items")
    if isinstance(items, (list, tuple, dict, set)) and len(items) == 0:
        return OUTCOME_EMPTY, None, "empty_result"

    category = obs.get("failure_category")
    if isinstance(category, str) and category in FAILURE_CATEGORIES:
        if category in (CATEGORY_ACCESS_DENIED, CATEGORY_CHALLENGE):
            return OUTCOME_BLOCKED, category, "declared_failure"
        return OUTCOME_TEMPORARY, category, "declared_failure"

    return OUTCOME_OK, None, "observed"


# ---------------------------------------------------------------------------
# 运行期状态（来源级：熔断、限速、条件请求校验器）
# ---------------------------------------------------------------------------


@dataclass
class SourceRuntime:
    failures: int = 0
    breaker_state: str = BREAKER_CLOSED
    opened_until: float = 0.0
    next_probe_at: float = 0.0
    next_allowed_at: float = 0.0
    escalated_to_render: bool = False
    render_hold_remaining: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "breaker_state": self.breaker_state,
            "opened_until": self.opened_until,
            "next_probe_at": self.next_probe_at,
            "next_allowed_at": self.next_allowed_at,
            "escalated_to_render": self.escalated_to_render,
            "render_hold_remaining": self.render_hold_remaining,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "last_outcome": self.last_outcome,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SourceRuntime":
        data = dict(data or {})
        runtime = cls()
        for name in (
            "failures",
            "breaker_state",
            "opened_until",
            "next_probe_at",
            "next_allowed_at",
            "escalated_to_render",
            "render_hold_remaining",
            "etag",
            "last_modified",
            "last_outcome",
        ):
            if name in data:
                setattr(runtime, name, data[name])
        return runtime


# ---------------------------------------------------------------------------
# 决策对象
# ---------------------------------------------------------------------------


@dataclass
class FetchDecision:
    source: str
    policy_version: str
    policy_fingerprint: str
    batch_id: str | None = None
    position: int | None = None
    request_kind: str = "plain"  # plain | render | none
    request_reason: str = "policy_plain"
    conditional: bool = False
    request_headers: dict[str, str] = field(default_factory=dict)
    outcome: str = OUTCOME_OK
    failure_category: str | None = None
    outcome_reason: str = ""
    http_status: int | None = None
    rate_limited: bool = False
    rate_limit_wait_seconds: float = 0.0
    breaker_state: str = BREAKER_CLOSED
    breaker_probe: bool = False
    breaker_probe_allowed: bool = False
    breaker_open_until: float | None = None
    breaker_probe_at: float | None = None
    retry_count: int = 0
    retry_delay_seconds: float = 0.0
    retry_scheduled: bool = False
    escalation: dict[str, Any] | None = None
    observed_at: float = 0.0

    # 兼容下游阅读：决策版本即策略集版本。
    @property
    def decision_version(self) -> str:
        return self.policy_version

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "policy_version": self.policy_version,
            "decision_version": self.decision_version,
            "policy_fingerprint": self.policy_fingerprint,
            "batch_id": self.batch_id,
            "position": self.position,
            "resume_position": self.position if self.position is not None else 0,
            "request_kind": self.request_kind,
            "request_reason": self.request_reason,
            "conditional": self.conditional,
            "request_headers": dict(self.request_headers),
            "outcome": self.outcome,
            "failure_category": self.failure_category,
            "outcome_reason": self.outcome_reason,
            "http_status": self.http_status,
            "rate_limited": self.rate_limited,
            "rate_limit_wait_seconds": self.rate_limit_wait_seconds,
            "breaker_state": self.breaker_state,
            "breaker_probe": self.breaker_probe,
            "breaker_probe_allowed": self.breaker_probe_allowed,
            "breaker_open_until": self.breaker_open_until,
            "breaker_probe_at": self.breaker_probe_at,
            "retry_count": self.retry_count,
            "retry_delay_seconds": self.retry_delay_seconds,
            "retry_scheduled": self.retry_scheduled,
            "escalation": self.escalation,
            "observed_at": self.observed_at,
        }


# ---------------------------------------------------------------------------
# 采集会话：跨记录/跨重启保留熔断、限速与条件请求状态
# ---------------------------------------------------------------------------


class Session:
    """来源级采集会话。

    ``now`` 可以是返回 epoch 秒的可调用对象（便于用虚拟时钟测试），
    也可以是固定数值；默认使用 ``time.time``。
    """

    def __init__(
        self,
        policies: PolicySet | None = None,
        now: Callable[[], float] | float | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        self.policies = policies or default_policies()
        if now is None:
            self._clock: Callable[[], float] = time.time
        elif callable(now):
            self._clock = now
        else:
            fixed = float(now)
            self._clock = lambda: fixed
        self.runtime: dict[str, SourceRuntime] = {}
        self.trace: list[dict[str, Any]] = []
        self.import_state(state or {})

    # -- 时钟与状态 ---------------------------------------------------------
    def now(self) -> float:
        return float(self._clock())

    def _runtime(self, source: str) -> SourceRuntime:
        name = normalize_source(source)
        if name not in self.runtime:
            self.runtime[name] = SourceRuntime()
        return self.runtime[name]

    def export_state(self) -> dict[str, Any]:
        return {
            "saved_at": self.now(),
            "sources": {
                name: runtime.to_dict() for name, runtime in sorted(self.runtime.items())
            },
        }

    def import_state(self, state: Mapping[str, Any] | None) -> None:
        if not state:
            return
        sources = state.get("sources", state if "sources" not in state else {})
        if not isinstance(sources, Mapping):
            return
        for name, data in sources.items():
            if isinstance(data, Mapping):
                self.runtime[normalize_source(name)] = SourceRuntime.from_dict(data)

    # -- 主决策 -------------------------------------------------------------
    def observe(
        self,
        record: Mapping[str, Any],
        position: int | None = None,
        batch_id: str | None = None,
    ) -> FetchDecision:
        source = normalize_source(record.get("src") or record.get("source"))
        policy = self.policies.for_source(source)
        runtime = self._runtime(source)
        now = self.now()
        obs = read_observation(record)

        decision = FetchDecision(
            source=source,
            policy_version=self.policies.version,
            policy_fingerprint=policy.fingerprint(),
            batch_id=batch_id,
            position=position,
            observed_at=now,
        )

        # 1) 熔断闸门：开启期间默认不发请求，除非策略允许半开探测。
        decision.breaker_open_until = runtime.opened_until or None
        decision.breaker_probe_at = runtime.next_probe_at or None
        decision.breaker_probe_allowed = bool(policy.circuit_breaker.allow_probe)
        gate = self._breaker_gate(runtime, policy, now)
        if gate is not None:
            decision.breaker_state = BREAKER_OPEN
            decision.breaker_probe = False
            decision.breaker_probe_at = runtime.next_probe_at or None
            decision.request_kind = "none"
            decision.request_reason = "circuit_open"
            decision.outcome = OUTCOME_TEMPORARY
            decision.failure_category = CATEGORY_CIRCUIT_OPEN
            decision.outcome_reason = "circuit_open"
            runtime.last_outcome = decision.outcome
            self._record(decision)
            return decision

        decision.breaker_state = runtime.breaker_state
        decision.breaker_probe = runtime.breaker_state == BREAKER_HALF_OPEN

        # 2) 限速：超过最大等待预算就不发请求（记录为暂时不可用，不碰状态）。
        wait = max(0.0, runtime.next_allowed_at - now)
        decision.rate_limit_wait_seconds = round(wait, 6)
        decision.rate_limited = wait > 0
        if wait > policy.rate_limit.max_wait_seconds:
            decision.request_kind = "none"
            decision.request_reason = "rate_limit_wait_exceeds_budget"
            decision.outcome = OUTCOME_TEMPORARY
            decision.failure_category = CATEGORY_RATE_LIMITED
            decision.outcome_reason = "rate_limit_deferred"
            runtime.last_outcome = decision.outcome
            self._record(decision)
            return decision

        # 3) 请求方式：策略优先渲染 / 之前被拦截后升级渲染 / 普通请求。
        if policy.request_mode == "render":
            decision.request_kind = "render"
            decision.request_reason = "policy_render_first"
        elif runtime.escalated_to_render:
            decision.request_kind = "render"
            decision.request_reason = "escalated_after_block"
        else:
            decision.request_kind = "plain"
            decision.request_reason = "policy_plain"
        if _bool(obs.get("render_required")) and decision.request_kind == "plain":
            decision.request_kind = "render"
            decision.request_reason = "render_required_signal"
            runtime.escalated_to_render = True
            runtime.render_hold_remaining = policy.render_hold_records

        # 4) 条件请求：只在普通请求上带校验器。
        headers: dict[str, str] = {}
        if policy.conditional_requests and decision.request_kind == "plain":
            if runtime.etag:
                headers["If-None-Match"] = runtime.etag
            if runtime.last_modified:
                headers["If-Modified-Since"] = runtime.last_modified
        decision.conditional = bool(headers)
        decision.request_headers = headers

        # 5) 归类结果。
        outcome, category, reason = classify_observation(obs, policy)
        decision.outcome = outcome
        decision.failure_category = category
        decision.outcome_reason = reason
        decision.http_status = obs.get("http_status")
        decision.retry_count, decision.retry_delay_seconds = self._retry_plan(
            policy, obs, outcome, category
        )
        decision.retry_scheduled = decision.retry_count > 0

        # 6) 被拦截的普通请求：按策略升级到渲染，并留下可追踪选择。
        if (
            outcome == OUTCOME_BLOCKED
            and decision.request_kind == "plain"
            and policy.render_on_block
        ):
            runtime.escalated_to_render = True
            runtime.render_hold_remaining = policy.render_hold_records
            decision.escalation = {
                "requested": True,
                "from": "plain",
                "to": "render",
                "reason": "blocked_plain",
                "next_request": "render",
                "hold_records": policy.render_hold_records,
            }
        elif (
            outcome in (OUTCOME_OK, OUTCOME_NOT_MODIFIED)
            and decision.request_kind == "plain"
        ):
            runtime.escalated_to_render = False
            runtime.render_hold_remaining = None
            decision.escalation = None
        else:
            decision.escalation = None

        # 渲染保持计数：暂态拦截不强制永久渲染，策略可限定恢复普通请求的时机。
        if decision.request_kind == "render" and runtime.render_hold_remaining is not None:
            runtime.render_hold_remaining -= 1
            if runtime.render_hold_remaining <= 0:
                runtime.escalated_to_render = False
                runtime.render_hold_remaining = None

        # 7) 回写校验器（只在看到可信内容时）。
        if outcome in (OUTCOME_OK, OUTCOME_NOT_MODIFIED):
            if obs.get("etag"):
                runtime.etag = str(obs["etag"])
            if obs.get("last_modified"):
                runtime.last_modified = str(obs["last_modified"])

        # 8) 更新熔断与限速状态。
        self._update_breaker(runtime, policy, now, outcome, decision.breaker_probe)
        runtime.next_allowed_at = now + policy.rate_limit.min_interval_seconds
        runtime.last_outcome = outcome
        decision.breaker_state = runtime.breaker_state
        decision.breaker_open_until = runtime.opened_until or None
        decision.breaker_probe_at = runtime.next_probe_at or None

        self._record(decision)
        return decision

    # -- 内部 ---------------------------------------------------------------
    def _breaker_gate(
        self, runtime: SourceRuntime, policy: SourcePolicy, now: float
    ) -> str | None:
        if runtime.breaker_state != BREAKER_OPEN:
            return None
        if now >= runtime.next_probe_at and policy.circuit_breaker.allow_probe:
            runtime.breaker_state = BREAKER_HALF_OPEN
            return None
        return BREAKER_OPEN

    def _update_breaker(
        self,
        runtime: SourceRuntime,
        policy: SourcePolicy,
        now: float,
        outcome: str,
        was_probe: bool,
    ) -> None:
        if outcome in OBSERVED_OUTCOMES:
            runtime.failures = 0
            runtime.breaker_state = BREAKER_CLOSED
            runtime.opened_until = 0.0
            runtime.next_probe_at = 0.0
            return

        # blocked / temporarily_unavailable：累计失败。
        runtime.failures += 1
        breaker = policy.circuit_breaker
        if was_probe or runtime.failures >= breaker.failure_threshold:
            runtime.breaker_state = BREAKER_OPEN
            runtime.opened_until = now + breaker.cooldown_seconds
            runtime.next_probe_at = now + breaker.probe_delay()

    @staticmethod
    def _retry_plan(
        policy: SourcePolicy,
        obs: Mapping[str, Any],
        outcome: str,
        category: str | None,
    ) -> tuple[int, float]:
        if outcome != OUTCOME_TEMPORARY or category not in policy.retry.retry_categories:
            return 0, 0.0
        attempts = 1
        try:
            attempts = max(1, int(obs.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        remaining = max(0, policy.retry.max_attempts - attempts)
        retry_after = obs.get("retry_after")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = policy.retry.backoff_seconds
        else:
            delay = policy.retry.backoff_seconds * (2 ** max(0, attempts - 1))
        return remaining, float(delay)

    def _record(self, decision: FetchDecision) -> None:
        entry = decision.as_dict()
        entry["trace_event"] = "fetch_decision"
        self.trace.append(entry)


# ---------------------------------------------------------------------------
# 流水线阶段与便捷入口
# ---------------------------------------------------------------------------


def process(record: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """对单条记录表达来源级采集决策。

    兼容基线调用方式 ``process(record)``；传入 ``BatchContext`` 时可带上批次、
    位置、持久化会话（跨记录/跨重启的熔断与条件请求状态）。
    """
    rec = dict(record)
    batch_id = getattr(context, "batch_id", None)
    position = getattr(context, "position", None)
    policies = getattr(context, "policy_set", None) or default_policies()
    session = getattr(context, "session", None)
    now = getattr(context, "now", None) or None
    if session is None:
        session = Session(policies=policies, now=now)
    elif session.policies is None:
        session.policies = policies

    decision = session.observe(rec, position=position, batch_id=batch_id)
    payload = decision.as_dict()

    rec["fetch_decision"] = payload
    rec["_stage_fetch_policy"] = decision.outcome
    # 决策版本 / 失败类别 / 恢复位置显式交给下游（renderer/snapshot/持久化）。
    rec["policy_version"] = decision.policy_version
    rec["decision_version"] = decision.policy_version
    rec["failure_category"] = decision.failure_category
    rec["fetch_outcome"] = decision.outcome
    rec["resume_position"] = position if position is not None else 0
    rec["request_kind"] = decision.request_kind
    return rec


def decide(
    record: Mapping[str, Any],
    policies: PolicySet | None = None,
    session: Session | None = None,
    now: Callable[[], float] | float | None = None,
    position: int | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """不经过流水线的单条决策便捷入口（返回 JSON 友好字典）。"""
    session = session or Session(policies=policies, now=now)
    return session.observe(record, position=position, batch_id=batch_id).as_dict()


def run_batch(
    records: Iterable[Mapping[str, Any]],
    policies: PolicySet | None = None,
    now: Callable[[], float] | float | None = None,
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """顺序评估一组记录，保留来源级熔断/限速状态（测试与离线回放用）。"""
    session = Session(policies=policies, now=now)
    decisions = []
    for index, record in enumerate(records):
        decisions.append(
            session.observe(record, position=index, batch_id=batch_id).as_dict()
        )
    return decisions
