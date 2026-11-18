"""薄编排：loader → normalizer→fetch_policy→renderer→snapshot → keyer → store。不含业务判断。"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'fetch_policy', 'renderer', 'snapshot']

# 阶段通过 ``_emit`` 提供"需要落库的行"；没有该键时按原行为把整条记录落库。
_EMIT = "_emit"


def _loaders():
    chain = []
    for n in CHAIN_NAMES:
        mod = importlib.import_module("." + n, __package__)
        chain.append(mod.process)
    return chain


def _apply(chain, raw, cache=None):
    rec = dict(raw)
    if isinstance(cache, dict) and not isinstance(rec.get("cache_store"), dict):
        rec["cache_store"] = cache
    for fn in chain:
        rec = fn(rec)
    return rec

def _outcome(rec):
    return {
        "key": key_of(rec),
        "status": rec.get("render_status"),
        "page_kind": rec.get("page_kind"),
        "error_kind": rec.get("error_kind"),
    }


def _run(records, con, cache=None, detailed=False):
    own = con is None
    con = con or _store.connect()
    try:
        chain = _loaders()
        fed = 0
        emitted = 0
        skipped = 0
        outcomes = []
        for raw in records:
            rec = _apply(chain, raw, cache)
            fed += 1
            rows = rec.get(_EMIT)
            if rows is None:
                rows = [rec]
            if not rows:
                skipped += 1
            for row in rows:
                _store.upsert(con, Record(key=key_of(row), data=row))
                emitted += 1
            if detailed:
                outcome = _outcome(rec)
                outcome["rows"] = len(rows)
                outcomes.append(outcome)
        _store.commit(con)
        result = {"fed": fed, "rows": _store.count(con)}
        if detailed:
            result.update({"emitted": emitted, "skipped": skipped, "records": outcomes})
        return result
    finally:
        if own:
            con.close()


def run(con=None):
    """Run the offline fixture through the pipeline.

    Returns ``{"fed": n, "rows": m}`` — the historical contract is unchanged.
    """
    return _run(_loader.load(), con)


def run_detailed(con=None):
    """Like :func:`run`, plus per-record statuses and emitted/skipped counts."""
    return _run(_loader.load(), con, detailed=True)


def run_records(records, con=None, cache=None, detailed=True):
    """Run already-loaded records (offline fixtures / tests).

    ``cache`` may be a plain dict shared across calls to simulate a persistent
    cache across restarts.  Output is detailed by default because callers of
    this helper are normally interested in statuses/classification.
    """
    return _run(records, con, cache=cache, detailed=detailed)
