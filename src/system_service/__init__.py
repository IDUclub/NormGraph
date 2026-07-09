"""System endpoints package (health, logs, settings)."""

from src.system_service.router import system_router  # noqa: F401

__all__ = ["system_router"]
