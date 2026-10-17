"""Data Linking agent module."""


def __getattr__(name):
    if name == "DataLinkingAgent":
        from .agent import DataLinkingAgent

        return DataLinkingAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DataLinkingAgent"]
