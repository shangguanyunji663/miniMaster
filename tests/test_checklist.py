from minimaster.core.checklist import ChecklistItem, CompletionChecklist


def make_checklist():
    return CompletionChecklist(
        goal="demo",
        items=[ChecklistItem("C1", "文件存在"), ChecklistItem("C2", "输出正确")],
    )


def test_initial_state_incomplete():
    checklist = make_checklist()
    assert not checklist.is_complete
    assert len(checklist.unmet()) == 2


def test_apply_verdict_and_complete():
    checklist = make_checklist()
    checklist.apply_verdict(
        [
            {"item_id": "C1", "satisfied": True, "evidence": "a.txt 存在"},
            {"item_id": "C2", "satisfied": True, "evidence": "输出为 1..100"},
        ]
    )
    assert checklist.is_complete
    assert checklist.unmet() == []
    assert "[x]" in checklist.render()


def test_partial_verdict_keeps_unmet():
    checklist = make_checklist()
    checklist.apply_verdict([{"item_id": "C1", "satisfied": True, "evidence": "ok"}])
    assert [i.item_id for i in checklist.unmet()] == ["C2"]


def test_unknown_item_ignored():
    checklist = make_checklist()
    checklist.apply_verdict([{"item_id": "C99", "satisfied": True}])
    assert not checklist.is_complete
