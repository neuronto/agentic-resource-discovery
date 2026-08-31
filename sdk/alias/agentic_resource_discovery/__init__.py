"""agentic-resource-discovery — an alias for `ard-publish`.

This package exists so the tool is findable under the term people actually
search for. It installs and re-exports `ard_publish`; there is no separate
implementation to keep in step.

    from agentic_resource_discovery import Manifest, Entry

is exactly

    from ard_publish import Manifest, Entry
"""
from ard_publish import (  # noqa: F401
    Entry,
    Manifest,
    ValidationError,
    validate,
    __version__,
)

__all__ = ["Entry", "Manifest", "ValidationError", "validate", "__version__"]
