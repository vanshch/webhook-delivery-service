"""Validation rules for external webhook event identifiers."""

import re

EVENT_ID_REGEX = re.compile(r"[a-zA-Z0-9_:-]+")
MIN_EVENT_ID_LENGTH = 1
MAX_EVENT_ID_LENGTH = 128


def validate_event_id(event_id: str) -> str:
    """Return an event ID after validating its length and safe character set."""
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("Event ID must be a non-empty string")
    if len(event_id) < MIN_EVENT_ID_LENGTH or len(event_id) > MAX_EVENT_ID_LENGTH:
        raise ValueError(
            f"Event ID length must be between {MIN_EVENT_ID_LENGTH} "
            f"and {MAX_EVENT_ID_LENGTH} characters"
        )
    if not EVENT_ID_REGEX.fullmatch(event_id):
        raise ValueError(
            "Event ID contains invalid characters. Only alphanumeric, '_', '-', "
            "and ':' are allowed."
        )
    return event_id
