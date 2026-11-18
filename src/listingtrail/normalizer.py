"""单一职责：字段规整/规范化（对记录做单一职责变换：normalizer）。规范阶段。"""
def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = _to_int(rec.get("value", 0))
    return rec
