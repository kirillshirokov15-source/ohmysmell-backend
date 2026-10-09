"""Closed vocabulary for startup diagnostics: never log exception messages."""


class WorkerConfigurationError(ValueError):
    def __init__(self, code, message=None):
        self.code = code if code in {"invalid_worker_role", "production_activation_required", "database_url_missing",
            "external_writes_forbidden", "manager_group_configuration_invalid", "manager_bot_token_missing",
            "client_bot_token_missing", "bot_roles_share_identity", "client_worker_disabled", "invalid_worker_port"} else "runtime_configuration_invalid"
        super().__init__(message or self.code)


def startup_error_category(error):
    if isinstance(error, WorkerConfigurationError):
        return error.code
    from app.services.desk_delivery_policy import DeskDeliveryConfigurationError
    if isinstance(error, DeskDeliveryConfigurationError):
        return error.code
    from aiogram.utils.token import TokenValidationError
    from aiogram.exceptions import TelegramUnauthorizedError, TelegramConflictError
    if isinstance(error, TokenValidationError):
        return "invalid_bot_token_format"
    if isinstance(error, TelegramUnauthorizedError):
        return "bot_token_rejected"
    if isinstance(error, TelegramConflictError):
        return "telegram_polling_conflict"
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "dependency_unavailable"
    if isinstance(error, ValueError):
        return "runtime_configuration_invalid"
    return "worker_runtime_failure"
