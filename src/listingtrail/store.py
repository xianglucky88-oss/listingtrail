"""单一职责：本地 sqlite3 持久化。

除了幂等的房源行（``records``），这里还负责让采集过程可追踪：

* ``fetch_trace``      每次来源观测一条：决策版本、失败类别、恢复位置、请求方式。
* ``batch_checkpoint`` 批次进度与会话状态（熔断/限速/条件请求校验器），支持中断后续跑。
* ``policy_versions``  策略配置版本的首见/末见，用来识别“配置更新时批次尚未完成”。
* ``events``           批次级事件（恢复、配置变更、中断），只追加不改写。

房源合并规则保证既有挂牌身份与状态语义不被降级观测破坏。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Mapping

from .config import DB_PATH
from .fetch_policy import DEGRADED_OUTCOMES


def _ensure_column(con, table, column, decl) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS fetch_trace("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        "batch_id TEXT, position INTEGER, key TEXT, source TEXT,"
        "policy_version TEXT, decision_version TEXT, outcome TEXT,"
        "failure_category TEXT, request_kind TEXT, payload TEXT, created_at TEXT)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fetch_trace_batch_pos_key "
        "ON fetch_trace(batch_id, position, key)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS batch_checkpoint("
        "batch_id TEXT PRIMARY KEY, policy_version TEXT, position INTEGER,"
        "total INTEGER, status TEXT, session_state TEXT,"
        "resume_from INTEGER, feed_digest TEXT, started_at TEXT, updated_at TEXT)"
    )
    _ensure_column(con, "batch_checkpoint", "feed_digest", "TEXT")
    con.execute(
        "CREATE TABLE IF NOT EXISTS policy_versions("
        "version TEXT PRIMARY KEY, payload TEXT, first_seen TEXT,"
        "last_seen TEXT, runs INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, kind TEXT,"
        "payload TEXT, created_at TEXT)"
    )
    return con


# ---------------------------------------------------------------------------
# 房源记录：合并式幂等 upsert
# ---------------------------------------------------------------------------


def _merge_identity(prior: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    prior_identity = prior.get("listing_identity")
    prior_identity = dict(prior_identity) if isinstance(prior_identity, Mapping) else {}
    new_identity = new.get("listing_identity")
    new_identity = dict(new_identity) if isinstance(new_identity, Mapping) else {}
    if not prior_identity.get("key"):
        return new_identity
    merged = dict(new_identity)
    # 身份键一经确定就不再重算：这是“不得改变已有挂牌身份语义”的落地点。
    merged["key"] = prior_identity["key"]
    merged.setdefault("stable_since", prior_identity.get("stable_since"))
    aliases = list(prior_identity.get("aliases") or [])
    for alias in new_identity.get("aliases") or []:
        if alias not in aliases:
            aliases.append(alias)
    merged["aliases"] = aliases
    return merged


def merge(prior: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    """把一次新观测合并进已有房源。"""
    merged = dict(new)
    if not prior:
        return merged

    if prior.get("listing_identity") or new.get("listing_identity"):
        identity = _merge_identity(prior, new)
        if identity:
            merged["listing_identity"] = identity
            merged["identity_key"] = identity.get("key", new.get("identity_key"))

    outcome = new.get("fetch_outcome")
    degraded = outcome in DEGRADED_OUTCOMES
    prior_snapshot = prior.get("listing_snapshot")
    prior_snapshot = prior_snapshot if isinstance(prior_snapshot, Mapping) else {}
    last_good = prior.get("last_good_snapshot")
    if not isinstance(last_good, Mapping):
        last_good = prior_snapshot if prior_snapshot.get("credible") else None

    if degraded:
        # 降级观测不产生状态事实：状态沿用上一次可信值，但保留“本次未观测”的轨迹。
        merged["listing_status"] = prior.get("listing_status", new.get("listing_status"))
        history = new.get("status_history")
        if not isinstance(history, list) or not history:
            history = prior.get("status_history")
        if history is not None:
            merged["status_history"] = history
        merged["status_observed"] = False
        merged["status_transition"] = None
        snapshot = dict(new.get("listing_snapshot") or {})
        snapshot["credible"] = False
        snapshot["ok"] = False
        snapshot["delisting_fact"] = False
        if last_good:
            snapshot["last_good_snapshot"] = last_good
            merged["last_good_snapshot"] = last_good
        merged["listing_snapshot"] = snapshot
        if not new.get("rendered_items") and prior.get("rendered_items"):
            # 降级观测没有内容：保留上次渲染结果，别把已有房源清空。
            merged["rendered_items"] = prior["rendered_items"]
    else:
        snapshot = new.get("listing_snapshot")
        if isinstance(snapshot, Mapping) and snapshot.get("credible"):
            merged["last_good_snapshot"] = snapshot
        elif last_good:
            merged["last_good_snapshot"] = last_good
        if (
            outcome == "not_modified"
            and not new.get("rendered_items")
            and prior.get("rendered_items")
        ):
            # 304 没有新内容：沿用上次渲染条目，避免把房源渲染成空列表。
            merged["rendered_items"] = prior["rendered_items"]
    return merged


def upsert(con, record):
    key = record.key
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    prior = json.loads(row[0]) if row else None
    data = merge(prior, record.data)
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (key, json.dumps(data, ensure_ascii=False)),
    )
    return data


def get(con, key) -> dict[str, Any] | None:
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# 决策轨迹 / 批次检查点 / 策略版本 / 事件
# ---------------------------------------------------------------------------


def _now_iso(now=None) -> str:
    if now is None:
        seconds = time.time()
    elif callable(now):
        seconds = now()
    else:
        seconds = float(now)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def record_decision(con, key, data: Mapping[str, Any], batch_id=None,
                    position=None, now=None) -> None:
    decision = data.get("fetch_decision")
    if not isinstance(decision, Mapping):
        return
    con.execute(
        "INSERT INTO fetch_trace(batch_id, position, key, source, policy_version,"
        " decision_version, outcome, failure_category, request_kind, payload, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(batch_id, position, key) DO UPDATE SET"
        " source=excluded.source, policy_version=excluded.policy_version,"
        " decision_version=excluded.decision_version, outcome=excluded.outcome,"
        " failure_category=excluded.failure_category,"
        " request_kind=excluded.request_kind, payload=excluded.payload,"
        " created_at=excluded.created_at",
        (
            batch_id,
            position,
            key,
            decision.get("source"),
            decision.get("policy_version"),
            decision.get("decision_version") or decision.get("policy_version"),
            decision.get("outcome"),
            decision.get("failure_category"),
            decision.get("request_kind"),
            json.dumps(decision, ensure_ascii=False),
            _now_iso(now),
        ),
    )


def fetch_trace(con, batch_id=None) -> list[dict[str, Any]]:
    if batch_id is None:
        rows = con.execute(
            "SELECT batch_id, position, key, source, policy_version,"
            " decision_version, outcome, failure_category, request_kind, payload"
            " FROM fetch_trace ORDER BY seq"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT batch_id, position, key, source, policy_version,"
            " decision_version, outcome, failure_category, request_kind, payload"
            " FROM fetch_trace WHERE batch_id=? ORDER BY position, seq",
            (batch_id,),
        ).fetchall()
    return [
        {
            "batch_id": r[0],
            "position": r[1],
            "key": r[2],
            "source": r[3],
            "policy_version": r[4],
            "decision_version": r[5],
            "outcome": r[6],
            "failure_category": r[7],
            "request_kind": r[8],
            "payload": json.loads(r[9]) if r[9] else {},
        }
        for r in rows
    ]


def save_checkpoint(con, batch_id, policy_version=None, position=-1, total=None,
                    status="in_progress", session_state=None,
                    resume_from=None, feed_digest=None, now=None) -> None:
    stamp = _now_iso(now)
    started = con.execute(
        "SELECT started_at FROM batch_checkpoint WHERE batch_id=?", (batch_id,)
    ).fetchone()
    started_at = started[0] if started and started[0] else stamp
    con.execute(
        "INSERT INTO batch_checkpoint(batch_id, policy_version, position, total,"
        " status, session_state, resume_from, feed_digest, started_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(batch_id) DO UPDATE SET"
        " policy_version=excluded.policy_version, position=excluded.position,"
        " total=excluded.total, status=excluded.status,"
        " session_state=excluded.session_state,"
        " resume_from=COALESCE(excluded.resume_from, batch_checkpoint.resume_from),"
        " feed_digest=COALESCE(excluded.feed_digest, batch_checkpoint.feed_digest),"
        " updated_at=excluded.updated_at",
        (
            batch_id,
            policy_version,
            position,
            total,
            status,
            json.dumps(session_state or {}, ensure_ascii=False),
            resume_from,
            feed_digest,
            started_at,
            stamp,
        ),
    )


def _checkpoint_row(row) -> dict[str, Any]:
    return {
        "batch_id": row[0],
        "policy_version": row[1],
        "position": row[2],
        "total": row[3],
        "status": row[4],
        "session_state": json.loads(row[5]) if row[5] else {},
        "resume_from": row[6],
        "feed_digest": row[7],
        "started_at": row[8],
        "updated_at": row[9],
    }


_CHECKPOINT_COLUMNS = (
    "batch_id, policy_version, position, total, status, session_state,"
    " resume_from, feed_digest, started_at, updated_at"
)


def latest_incomplete(con) -> dict[str, Any] | None:
    # 同一秒内多次更新时用 rowid 兜底，保证“最近写入”的批次优先。
    row = con.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM batch_checkpoint"
        " WHERE status='in_progress' ORDER BY updated_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _checkpoint_row(row) if row else None


def checkpoint(con, batch_id) -> dict[str, Any] | None:
    row = con.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM batch_checkpoint WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    return _checkpoint_row(row) if row else None


def record_policy_version(con, policy_set, now=None) -> None:
    version = policy_set.version
    payload = json.dumps(policy_set.as_dict(), ensure_ascii=False)
    stamp = _now_iso(now)
    con.execute(
        "INSERT INTO policy_versions(version, payload, first_seen, last_seen, runs)"
        " VALUES(?,?,?,?,1)"
        " ON CONFLICT(version) DO UPDATE SET last_seen=excluded.last_seen,"
        " runs=policy_versions.runs+1, payload=excluded.payload",
        (version, payload, stamp, stamp),
    )


def policy_versions(con) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT version, payload, first_seen, last_seen, runs"
        " FROM policy_versions ORDER BY first_seen"
    ).fetchall()
    return [
        {
            "version": r[0],
            "payload": json.loads(r[1]) if r[1] else {},
            "first_seen": r[2],
            "last_seen": r[3],
            "runs": r[4],
        }
        for r in rows
    ]


def record_event(con, kind, payload=None, batch_id=None, now=None) -> None:
    con.execute(
        "INSERT INTO events(batch_id, kind, payload, created_at) VALUES(?,?,?,?)",
        (batch_id, kind, json.dumps(payload or {}, ensure_ascii=False), _now_iso(now)),
    )


def events(con, batch_id=None) -> list[dict[str, Any]]:
    if batch_id is None:
        rows = con.execute(
            "SELECT batch_id, kind, payload, created_at FROM events ORDER BY seq"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT batch_id, kind, payload, created_at FROM events"
            " WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ).fetchall()
    return [
        {
            "batch_id": r[0],
            "kind": r[1],
            "payload": json.loads(r[2]) if r[2] else {},
            "created_at": r[3],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 基础操作
# ---------------------------------------------------------------------------


def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
