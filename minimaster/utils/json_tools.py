"""从模型回复中稳健提取 JSON 对象（裸 JSON / 代码块 / 首尾大括号兜底）。"""
from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict:
    """尽力解析出第一个合法 JSON 对象，失败抛 ValueError。"""
    if not text or not text.strip():
        raise ValueError("模型返回空内容，无法解析 JSON")
    candidates: list[str] = []
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates += [f.strip() for f in fences]
    stripped = text.strip()
    candidates.append(stripped)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"无法从模型回复中解析 JSON，原文片段: {text[:300]!r}")
