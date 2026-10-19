from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiogram.types import ForceReply, InlineKeyboardMarkup
from tests.test_manager_menu import dispatch


@pytest.mark.parametrize('button,action', [('Найти заказ', 'order'), ('Источники', 'sources')])
def test_number_reuses_handler_once(dispatch, monkeypatch, button, action):
    from app.bot import telegram_bot as bot
    handler = AsyncMock()
    monkeypatch.setattr(bot, 'find_order' if action == 'order' else 'sources_handler', handler)
    assert isinstance(dispatch(button)[0].reply_markup, ForceReply)
    assert 'положительный' in dispatch('abc')[0].text
    assert 'положительный' in dispatch('0')[0].text
    dispatch('16')
    handler.assert_awaited_once()
    message = handler.call_args.args[0]
    assert message.text == f'/{action} 16' and message.from_user.id == 11
    assert dispatch('16') == []


def test_existing_search_card_and_missing_order(dispatch, monkeypatch):
    from app.bot import telegram_bot as bot
    async def publish(transport, chat_id, entity, text, keyboard):
        return await transport.send_message(chat_id, text, reply_markup=keyboard)
    monkeypatch.setattr("app.services.telegram_cards.publish", publish)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, 'get_order', lookup)
    dispatch('Найти заказ')
    assert 'не найден' in dispatch('99')[0].text
    lookup.return_value = SimpleNamespace(id=16)
    monkeypatch.setattr(bot, 'shipments_for', AsyncMock(return_value=[]))
    monkeypatch.setattr(bot, 'supply_order_card', AsyncMock(return_value='Card'))
    monkeypatch.setattr(bot, 'order_keyboard', lambda *a: {'inline_keyboard': [[{'text':'Refresh','callback_data':'order:refresh:16:0'}]]})
    dispatch('Найти заказ')
    response = dispatch('16')[0]
    assert response.text == 'Card' and isinstance(response.reply_markup, InlineKeyboardMarkup)


def test_actual_sources_handler(dispatch):
    service = SimpleNamespace(read=AsyncMock(return_value={'order_id':16,'revision':0,'items':[],'allocations':[]}))
    dispatch.sources.return_value = service
    dispatch('Источники')
    assert '16' in dispatch('16')[0].text
    service.read.assert_awaited_once_with(16, 11, 11)


def test_group_two_actors_exact_reply_cancel_expiry(dispatch, monkeypatch):
    from app.bot import telegram_bot as bot, manager_dialog as dialog
    monkeypatch.setenv('MANAGER_TELEGRAM_CHAT_ID', '-100123')
    monkeypatch.setenv('MANAGER_TELEGRAM_USER_IDS', '11,12')
    handler = AsyncMock()
    monkeypatch.setattr(bot, 'find_order', handler)
    dispatch('Найти заказ', group=True)
    dispatch('Найти заказ', group=True, actor=12)
    assert len(dialog.pending) == 2
    assert dispatch('16', group=True) == []
    assert dispatch('16', group=True, reply=999) == []
    assert dispatch('обычное сообщение', group=True) == []
    dispatch('16', group=True, reply=1, actor=12)
    handler.assert_awaited_once()
    assert (-100123,11) in dialog.pending
    dispatch('/cancel', group=True)
    assert not dialog.pending
    dispatch('Найти заказ', group=True)
    monkeypatch.setattr(dialog.time, 'monotonic', lambda: 10**12)
    assert dispatch('16', group=True, reply=1) == []
    assert not dialog.pending


def test_privacy_fallback_is_actor_bound(dispatch, monkeypatch):
    monkeypatch.setenv('MANAGER_TELEGRAM_CHAT_ID', '-100123')
    monkeypatch.setenv('MANAGER_TELEGRAM_USER_IDS', '11,12')
    from app.bot.manager_dialog import pending
    menu = dispatch('/menu', group=True)[-1].reply_markup
    assert isinstance(menu, InlineKeyboardMarkup)
    payload = menu.inline_keyboard[0][1].callback_data
    dispatch('', group=True, callback=payload, actor=12)
    assert not pending
    calls = dispatch('', group=True, callback=payload)
    assert isinstance(calls[-1].reply_markup, ForceReply)
    assert calls[-1].reply_markup.selective
    dispatch.access.return_value = False
    dispatch('16', group=True, reply=2)
    assert not pending


def test_prompt_replacement_and_navigation(dispatch, monkeypatch):
    from app.bot import manager_dialog as dialog
    dispatch('Найти заказ')
    assert dispatch('16', actor=12) == []
    assert dispatch('16', chat_id=99) == []
    dispatch('Источники')
    assert dialog.pending[(11,11)][0] == 'sources'
    dispatch('/menu')
    assert not dialog.pending
    dispatch('Найти заказ')
    dispatch('Скрыть меню')
    assert not dialog.pending
