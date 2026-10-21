"""Project-level exception hierarchy."""


class DuctorError(Exception):
    """Base for all ductor exceptions."""


class ProductError(ValueError):
    """A fixed message the product writes for a person.

    ``str(error)`` is the English text (tools, logs and the internal API read that);
    :meth:`shown` is the same message in the configured language.
    """

    def __init__(self, kind: str, english: str, **params: object) -> None:
        super().__init__(english)
        self.kind = kind
        self.params = params

    def shown(self) -> str:
        from ductor_bot.i18n import t

        return t(f"product_error.{self.kind}", **self.params)


class CLIError(DuctorError):
    """CLI execution failed."""


class WorkspaceError(DuctorError):
    """Workspace initialization or access failed."""


class SessionError(DuctorError):
    """Session persistence or lifecycle failed."""


class CronError(DuctorError):
    """Cron job scheduling or execution failed."""


class StreamError(DuctorError):
    """Streaming output failed."""


class SecurityError(DuctorError):
    """Security violation detected."""


class PathValidationError(SecurityError):
    """File path failed validation."""


class WebhookError(DuctorError):
    """Webhook server or dispatch failed."""
