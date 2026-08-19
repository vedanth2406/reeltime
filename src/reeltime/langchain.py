"""Public surface for the LangChain adapter -- see :mod:`reeltime.core.langchain`.

Exists so that both spellings work::

    import reeltime as tape
    tape.langchain.install()

    from reeltime.langchain import install
"""

from __future__ import annotations

from .core.langchain import (
    BELOW,
    MINIMUM,
    check_version,
    handler,
    install,
    installed,
    recording,
    uninstall,
)

__all__ = [
    "install",
    "uninstall",
    "installed",
    "recording",
    "handler",
    "check_version",
    "MINIMUM",
    "BELOW",
]
