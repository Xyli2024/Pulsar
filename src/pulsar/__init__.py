"""pulsar — a live hardware dashboard for the terminal."""

__version__ = "0.1.0"

from .collector import collect, get_system_info

__all__ = ["collect", "get_system_info", "__version__"]
