import pytest

from minimaster.utils.json_tools import extract_json


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '```json\n{"tasks": [{"id": "T1"}]}\n```'
    assert extract_json(text) == {"tasks": [{"id": "T1"}]}


def test_embedded_json_with_prose():
    text = '好的，以下是规划：\n{"action": "final", "content": "done"}\n请查收。'
    assert extract_json(text)["action"] == "final"


def test_invalid_raises():
    with pytest.raises(ValueError):
        extract_json("这不是 JSON")
    with pytest.raises(ValueError):
        extract_json("")


def test_nested_braces():
    text = '{"a": {"b": {"c": 1}}, "d": "含 } 花括号"}'
    assert extract_json(text)["a"]["b"]["c"] == 1
