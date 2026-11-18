"""The chat path moved out of ``plugboard/app.py`` (PLU-95), one module per subject.

Each module that touches ``app.py`` exposes ``register(app, core) -> SimpleNamespace``:
it declares its routes on ``app`` with ``@app.<method>``, in their original order,
and returns the closures ``app.py`` re-exports. ``core`` is the ``plugboard.app``
module object that registers them, so every import of ``app.py`` binds its
handlers and helpers to itself.

- Any name that stayed in ``app.py``, and any name the tests patch on
  ``plugboard.app``, is read as ``core.<name>`` when the code runs, never bound
  at import, even when the patch point is defined in the same ``register()``
  (R3).
- Between modules of this package, a module-level name is imported directly
  (``from plugboard.chat_path.streaming import _ClientGone``); a closure is read
  as ``core.<name>``, its re-export in ``app.py`` (R10). No relative import.
- ``app.py`` imports this package last among its top imports; a module here only
  imports, at module level, modules ``app.py`` already imports at module level,
  so no ``plugboard`` module is loaded earlier or in addition (R11).
- A module that logs uses ``logging.getLogger("plugboard.app")``: same logger,
  same ``source_service``.
- A module never imports ``plugboard.app``: ``core`` is its only access to it.

Design: ``docs/superpowers/specs/2026-11-18-plu-95-chat-path-design.md``.
"""
