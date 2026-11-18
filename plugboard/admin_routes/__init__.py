"""Admin routes moved out of ``plugboard/app.py`` (PLU-36), one module per domain.

Each module exposes ``register(app, core) -> SimpleNamespace``: it declares its
routes on ``app`` with ``@app.<method>``, in their original order, and returns
the names ``app.py`` re-exports. ``core`` is the ``plugboard.app`` module object
that registers the routes, so every import of ``app.py`` binds its handlers to
itself.

- Any name that stayed in ``app.py``, and any name the tests patch on
  ``plugboard.app``, is read as ``core.<name>`` when the handler runs, never
  bound at import (``Depends(core._require_admin)`` included).
- A function that reads ``core`` is defined inside ``register()``; a name used
  in a handler annotation is a module global (FastAPI resolves string
  annotations in the module globals, not in the closure).
- A module that logs uses ``logging.getLogger("plugboard.app")``: same logger,
  same ``source_service``.
- A module never imports ``plugboard.app``: ``core`` is its only access to it.

Design: ``docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md``.
"""
