"""Errors shared across services. Messages are categories, never user content."""


class InvalidInput(ValueError):
    """The caller's input failed validation before any database work."""


class NotFound(InvalidInput):
    """The item the caller named does not exist in the namespace."""
