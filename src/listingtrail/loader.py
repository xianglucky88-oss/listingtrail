"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。"""
import json
from .config import FIXTURES


def load(path=None):
    """Load raw records from *path* (default: the configured fixture)."""
    target = FIXTURES if path is None else path
    with open(target, encoding="utf-8") as f:
        return json.load(f)
