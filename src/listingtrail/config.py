import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
DB_PATH = ROOT / "listingtrail.db"

#: 可选：来源级采集策略配置文件（JSON）。不存在时使用 fetch_policy 内置默认值。
POLICY_CONFIG = Path(os.environ.get("LISTINGTRAIL_POLICY", ROOT / "fetch_policy.json"))


def load_policy_overrides() -> dict:
    """读取策略配置覆盖；文件缺失或为空时返回空字典（使用内置默认策略）。"""
    if not POLICY_CONFIG.exists():
        return {}
    with open(POLICY_CONFIG, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}
