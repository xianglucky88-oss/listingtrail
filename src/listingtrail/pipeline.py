"""薄编排：loader → normalizer → fetch_policy → identity → renderer → statusline
→ snapshot → keyer → store。不含业务判断。

编排层只负责：

* 给每个阶段传一个 ``BatchContext``（批次、位置、采集会话、历史房源）；
* 在每条记录后写检查点，使中断/重启可恢复；
* 开工时对比检查点里的策略版本与本次策略版本，记录“配置变更时批次未完成”；
* 恢复期间从 ``resume_from`` 继续，并沿用已有的房源身份与状态语义。
"""
from __future__ import annotations

import importlib
import inspect
import uuid
from typing import Any, Iterable, Mapping

from . import loader as _loader
from . import store as _store
from .keyer import candidates, key_of
from .models import BatchContext, Record

CHAIN_NAMES = [
    "normalizer",
    "fetch_policy",
    "identity",
    "renderer",
    "statusline",
    "snapshot",
]


def _bind_stage(fn):
    """兼容 ``process(record)`` 与 ``process(record, context)`` 两种签名。"""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return lambda record, context: fn(record)
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return lambda record, context: fn(record, context)
    positional = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2 or any(
        p.kind == p.KEYWORD_ONLY and p.name == "context" for p in params
    ):
        return lambda record, context: fn(record, context)
    return lambda record, context: fn(record)


def _load_chain():
    chain = []
    for name in CHAIN_NAMES:
        module = importlib.import_module("." + name, __package__)
        chain.append((name, _bind_stage(module.process)))
    return chain


def _identity_key(record: Mapping[str, Any]) -> str:
    identity = record.get("listing_identity")
    if isinstance(identity, Mapping) and identity.get("key"):
        return str(identity["key"])
    return key_of(record)


def _find_prior(con, record: Mapping[str, Any]) -> dict[str, Any] | None:
    identity = record.get("listing_identity")
    if isinstance(identity, Mapping):
        for candidate in identity.get("lookup_keys") or []:
            prior = _store.get(con, candidate)
            if prior is not None:
                return prior
    for candidate in candidates(record):
        prior = _store.get(con, candidate)
        if prior is not None:
            return prior
    return None


def _new_batch_id() -> str:
    return "batch-" + uuid.uuid4().hex[:10]


def _default_policies():
    """内置默认策略 + 可选的本地策略配置覆盖（``fetch_policy.json``）。"""
    from . import config
    from .fetch_policy import load_policies

    return load_policies(config.load_policy_overrides())


def _feed_digest(feed: Iterable[Mapping[str, Any]]) -> str:
    """批次输入的形状+身份摘要：只有同一批数据才允许原批次号续跑。"""
    import hashlib
    import json

    parts = []
    for index, raw in enumerate(feed):
        identity = {
            name: raw.get(name)
            for name in ("id", "external_id", "listing_id", "source_listing_id",
                         "src", "url", "detail_url", "listing_url", "permalink")
            if raw.get(name) not in (None, "")
        }
        parts.append({"index": index, "identity": identity})
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def run(
    con=None,
    *,
    records: Iterable[Mapping[str, Any]] | None = None,
    policies=None,
    session=None,
    now=None,
    batch_id: str | None = None,
    resume: bool = True,
    start: int | None = None,
    stop_after: int | None = None,
) -> dict[str, Any]:
    """跑一批采集流水线。

    ``records`` 可注入（测试/回放）；默认读本地 fixtures。``now`` 可以是
    虚拟时钟，便于复现限速与熔断时间线。
    """
    from .fetch_policy import Session

    own = con is None
    con = con or _store.connect()
    try:
        policy_set = policies or _default_policies()
        _store.record_policy_version(con, policy_set, now=now)

        feed = list(records) if records is not None else list(_loader.load())
        total = len(feed)
        feed_digest = _feed_digest(feed)
        batch_id = batch_id or _new_batch_id()

        resume_from = 0
        recovered_state: dict[str, Any] | None = None
        checkpoint = None if not resume else _store.latest_incomplete(con)
        if checkpoint and (
            checkpoint.get("feed_digest") not in (None, feed_digest)
            or checkpoint.get("total") not in (None, total)
        ):
            # 待续跑批次与本次输入不一致：不冒险错位续跑，另起新批次并留痕。
            _store.record_event(
                con,
                "resume_incompatible",
                {
                    "batch_id": checkpoint["batch_id"],
                    "checkpoint_total": checkpoint.get("total"),
                    "feed_total": total,
                    "checkpoint_digest": checkpoint.get("feed_digest"),
                    "feed_digest": feed_digest,
                },
                batch_id=checkpoint["batch_id"],
                now=now,
            )
            checkpoint = None
        if checkpoint:
            resume_from = (checkpoint.get("position") or 0) + 1
            recovered_state = checkpoint.get("session_state") or {}
            _store.record_event(
                con,
                "batch_resumed",
                {
                    "batch_id": checkpoint["batch_id"],
                    "policy_version_at_checkpoint": checkpoint.get("policy_version"),
                    "policy_version_now": policy_set.version,
                    "position": checkpoint.get("position"),
                    "resume_from": resume_from,
                },
                batch_id=checkpoint["batch_id"],
                now=now,
            )
            if checkpoint.get("policy_version") != policy_set.version:
                # 配置在批次未完成时更新：旧记录身份/状态保持不变，新记录用新策略。
                _store.record_event(
                    con,
                    "policy_changed_mid_batch",
                    {
                        "batch_id": checkpoint["batch_id"],
                        "from_version": checkpoint.get("policy_version"),
                        "to_version": policy_set.version,
                        "resume_from": resume_from,
                        "total": checkpoint.get("total", total),
                    },
                    batch_id=checkpoint["batch_id"],
                    now=now,
                )
            # 恢复沿用原批次号，保证请求轨迹可比对。
            batch_id = checkpoint.get("batch_id") or batch_id
            total = checkpoint.get("total") or total

        if start is not None and start > resume_from:
            resume_from = start

        if session is None:
            session = Session(policies=policy_set, now=now, state=recovered_state)
        elif recovered_state:
            session.import_state(recovered_state)
        if session.policies is None:
            session.policies = policy_set

        _store.save_checkpoint(
            con,
            batch_id,
            policy_version=policy_set.version,
            position=resume_from - 1,
            total=total,
            status="in_progress",
            session_state=session.export_state(),
            resume_from=resume_from,
            feed_digest=feed_digest,
            now=now,
        )

        chain = _load_chain()
        fed = 0
        skipped_resumed = 0
        for position, raw in enumerate(feed):
            if position < resume_from:
                skipped_resumed += 1
                continue
            ctx = BatchContext(
                batch_id=batch_id,
                position=position,
                session=session,
                policy_set=policy_set,
                prior=_find_prior(con, raw),
                now=now if callable(now) else None,
            )
            rec = raw
            for name, stage in chain:
                rec = stage(rec, ctx)
                if name == "identity" and isinstance(rec, Mapping):
                    # 身份确定后再对齐一次历史记录：无编号房源可能刚拿到稳定键。
                    prior = _find_prior(con, rec)
                    if prior is not None:
                        ctx.prior = prior
            key = _identity_key(rec)
            stored = _store.upsert(con, Record(key=key, data=rec))
            _store.record_decision(
                con,
                key,
                stored,
                batch_id=batch_id,
                position=position,
                now=now,
            )
            _store.save_checkpoint(
                con,
                batch_id,
                policy_version=policy_set.version,
                position=position,
                total=total,
                status="in_progress",
                session_state=session.export_state(),
                now=now,
            )
            _store.commit(con)
            fed += 1
            if stop_after is not None and fed >= stop_after and position < total - 1:
                # 模拟批次中断：检查点保持 in_progress，供重启后续跑。
                _store.record_event(
                    con,
                    "batch_interrupted",
                    {
                        "batch_id": batch_id,
                        "position": position,
                        "total": total,
                        "policy_version": policy_set.version,
                    },
                    batch_id=batch_id,
                    now=now,
                )
                _store.commit(con)
                return {
                    "fed": fed,
                    "rows": _store.count(con),
                    "batch_id": batch_id,
                    "policy_version": policy_set.version,
                    "resume_from": resume_from,
                    "skipped_resumed": skipped_resumed,
                    "interrupted": True,
                    "resume_position": position,
                    "degraded": _count_degraded(con),
                }

        _store.save_checkpoint(
            con,
            batch_id,
            policy_version=policy_set.version,
            position=total - 1,
            total=total,
            status="completed",
            session_state=session.export_state(),
            feed_digest=feed_digest,
            now=now,
        )
        _store.commit(con)

        return {
            "fed": fed,
            "rows": _store.count(con),
            "batch_id": batch_id,
            "policy_version": policy_set.version,
            "resume_from": resume_from,
            "skipped_resumed": skipped_resumed,
            "degraded": _count_degraded(con),
        }
    finally:
        if own:
            con.close()


def _count_degraded(con) -> int:
    count = 0
    for _key, data in _store.all_records(con):
        snapshot = data.get("listing_snapshot") if isinstance(data, Mapping) else None
        if isinstance(snapshot, Mapping) and not snapshot.get("credible", True):
            count += 1
    return count
