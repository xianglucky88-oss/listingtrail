"""单一职责：对记录做单一职责变换：snapshot。

用于记录变换。"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_snapshot", "naive")
    return rec
