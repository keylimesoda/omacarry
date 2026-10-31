"""omacarry: carry local Omarchy plugin changes across upstream updates."""

from .core import (  # noqa: F401
    GitError,
    Plugin,
    Result,
    auto_pass,
    deploy,
    discover,
    fetch,
    inspect_plugin,
    rebaseline,
    resume,
    scan,
    set_upstream,
)
