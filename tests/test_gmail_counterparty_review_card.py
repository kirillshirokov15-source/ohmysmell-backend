"""Gmail draft #124: counterparties must actually be selectable in Telegram.

Provider search isn't assumed to be a verified identity. No calls to
MoySklad and no modifications to orders or warehouse documents.
"""
from types import SimpleNamespace as Draft
from app.services.draft_telegram_service import (
    build_draft_keyboard, compact_draft_keyboard,
    counterparty_review_card, counterparty_review_keyboard,
)


def draft(candidates=None, *, linked=None, revision=7):
    return Draft(id=124, revision=revision, status="needs_review",
                 source="email", contact_details={"mail_intent":"order"},
                 items=[], counterparty_id=linked,
                 counterparty_candidates=list(candidates or []))


def callbacks(keyboard):
    return [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]


def test_no_match_has_manual_search_create_retry_and_back_not_fake_results():
    d=draft()
    # This compact order keyboard was incorrectly attached to the old
    # dedicated review response, hiding all actual lookup buttons.
    assert not any(":cpmanual:" in c for c in callbacks(compact_draft_keyboard(d)))
    view=counterparty_review_card(d)
    menu=counterparty_review_keyboard(d)
    actions=callbacks(menu)
    assert "Точных совпадений не найдено" in view
    assert "Выберите контрагента из вариантов" not in view
    assert actions == [
        "draft:cpmanual:124:v7",
        "draft:cpnew:124:v7",
        "draft:counterparty_select:124:v7",
        "draft:refresh:124",
    ]
    assert all(len(c.encode()) <= 64 for c in actions)
    assert not any(":finalize:" in c or ":pick:" in c for c in actions)


def test_match_has_actionable_candidate_buttons_with_same_indices_and_revision():
    candidates=[
        {"id":"uuid-1","name":"ООО Ромашка","reason":"Email: buyer@company.test"},
        {"id":"uuid-2","name":"АО Ромашка","reason":"Название похоже"},
    ]
    d=draft(candidates)
    view=counterparty_review_card(d)
    actions=callbacks(counterparty_review_keyboard(d))
    assert "Найдено вариантов: 2" in view
    assert "1. ООО Ромашка" in view and "2. АО Ромашка" in view
    assert "ещё не подтверждены" in view
    assert actions[:2] == ["draft:cp:124:0:v7", "draft:cp:124:1:v7"]
    assert "draft:cpmanual:124:v7" in actions
    assert "draft:cpnew:124:v7" in actions
    assert "draft:counterparty_select:124:v7" in actions


def test_manual_no_results_has_accurate_message_and_search_controls():
    d=draft()
    view=counterparty_review_card(d,manual_search=True)
    actions=callbacks(counterparty_review_keyboard(d))
    assert "По ручному запросу контрагенты не найдены" in view
    assert actions[0]=="draft:cpmanual:124:v7"


def test_already_linked_counterparty_has_back_only():
    d=draft([{"id":"uuid-1","name":"ООО Ромашка"}],linked="uuid-1")
    assert callbacks(counterparty_review_keyboard(d))==["draft:refresh:124"]
    assert "Контрагент уже выбран" in counterparty_review_card(d)


def test_review_does_not_offer_email_management_to_tilda():
    d=draft()
    d.source="website"
    assert callbacks(counterparty_review_keyboard(d))==["draft:refresh:124"]
