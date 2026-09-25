"""One explicit shared group, with a separate actor allowlist."""
import os


def group_id():
    value = os.getenv("MANAGER_TELEGRAM_CHAT_ID", "").strip()
    return int(value) if value else None


def allowed_users():
    return {int(v.strip()) for v in os.getenv("MANAGER_TELEGRAM_USER_IDS", "").split(",") if v.strip()}


def validate_group_config():
    mode = os.getenv('MANAGER_TELEGRAM_MODE', 'auto')
    if mode not in ('auto', 'private', 'group'):
        raise ValueError('MANAGER_TELEGRAM_MODE must be auto, private or group')
    try:
        chat, users = group_id(), allowed_users()
    except ValueError:
        raise ValueError('Telegram chat/user IDs must be integers') from None
    if mode == 'private' and (chat is not None or users):
        raise ValueError('Private mode cannot contain group configuration')
    if mode == 'group' or chat is not None or users:
        if chat is None or chat >= 0:
            raise ValueError('Group mode requires a negative MANAGER_TELEGRAM_CHAT_ID')
        if not users or any(user <= 0 for user in users):
            raise ValueError('Group mode requires positive MANAGER_TELEGRAM_USER_IDS')
    return 'group' if chat is not None else 'private'


def allowed_event(event, callback=False):
    message = event.message if callback else event
    chat = getattr(message, "chat", None)
    actor = getattr(event, "from_user", None)
    configured = group_id()
    if configured is None:
        return getattr(chat, "type", "private") == "private"
    if not chat or chat.id != configured or not actor or actor.id not in allowed_users():
        return False
    # Anonymous admin posts cannot identify the human actor.
    if not callback and getattr(message, "sender_chat", None):
        return False
    return callback or bool(getattr(message, "text", "") and message.text.startswith("/"))


def notification_chats(managers):
    configured = group_id()
    if configured is not None:
        if not allowed_users():
            raise ValueError("Group mode requires MANAGER_TELEGRAM_USER_IDS")
        return [configured]
    return [m.telegram_id for m in managers]
