"""UniVerse — a local-first university learning workspace."""

import concurrent.futures as _futures

# On Python 3.15, once PySide6 installs its import hook, the lazily resolved
# `concurrent.futures.ThreadPoolExecutor` stays an unresolved lazy object and
# LanceDB fails at import. Touching it here, before any Qt import, resolves it.
_futures.ThreadPoolExecutor

__version__ = "0.1.0"
