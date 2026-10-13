"""ASTRA standalone backend package."""
from .version import __version__, APP_VERSION, APP_NAME, APP_NAME_EN

__all__ = ["settings", "__version__", "APP_VERSION", "APP_NAME", "APP_NAME_EN"]


def __getattr__(name):
    # Protocol/CLI imports must not resolve configuration or credentials.
    if name == "settings":
        from .config import settings
        return settings
    raise AttributeError(name)
