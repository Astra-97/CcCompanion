"""Contact-owned routing boundary for the shared APNs server.

The HTTP server deliberately owns authentication, JSON parsing, history and
attachment staging.  This package owns the mapping from a contact identity to
its provider-specific endpoint and turn handler.  Modules use duck-typed
``handler`` objects so they never import :mod:`push` (and therefore cannot
create an import cycle with the long-lived server process).
"""

from .registry import (
    chat_contact_directory,
    default_contact_routes,
    dispatch_contact_get,
    dispatch_contact_post,
    dispatch_contact_send,
    dispatch_contact_stop,
)
from .xiaoke import clean_private_metadata as clean_xiaoke_private_metadata

__all__ = [
    "chat_contact_directory",
    "default_contact_routes",
    "dispatch_contact_get",
    "dispatch_contact_post",
    "dispatch_contact_send",
    "dispatch_contact_stop",
    "clean_xiaoke_private_metadata",
]
