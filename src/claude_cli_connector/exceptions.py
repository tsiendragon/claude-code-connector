"""Custom exceptions for claude-cli-connector."""


class ConnectorError(Exception):
    """Base exception for all connector errors."""


class SessionNotFoundError(ConnectorError):
    """Raised when a requested Claude session cannot be found."""


class SessionTimeoutError(ConnectorError):
    """Raised when waiting for Claude to become ready exceeds the timeout."""


class TransportError(ConnectorError):
    """Raised when the underlying tmux transport encounters an error."""


class SessionAlreadyExistsError(ConnectorError):
    """Raised when trying to create a session with a name that already exists."""
