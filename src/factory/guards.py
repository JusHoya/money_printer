"""Wall-clock tripwire for factory workers (PRD_STRATEGY_FACTORY FR-F2.5).

Inside a worker (and anywhere :func:`install` has run) the wall-clock readers

* ``time.time`` / ``time.time_ns`` / ``time.monotonic`` / ``time.monotonic_ns``
* ``datetime.datetime.now`` / ``utcnow`` / ``today``
* ``pandas.Timestamp.now`` / ``today`` / ``utcnow`` (when pandas is importable)

raise :class:`WallClockError` when the CALLING frame's code object lives in
``src/factory/genome.py``, ``src/factory/features.py`` or anywhere under
``src/strategies/``. Every other caller keeps working (the ledger and status
writers do not read the clock at all -- the guard is a tripwire for the code
that must be a pure function of the frame, ``docs/factory/FACTORY_ARCHITECTURE.md``
section 7.3 and section 11).

Mechanism
---------
``time`` functions are replaced by thin wrappers that inspect
``sys._getframe`` before delegating. ``datetime.datetime`` is a builtin whose
attributes cannot be patched, so :func:`install` swaps ``datetime.datetime``
for a subclass whose ``now``/``utcnow``/``today`` classmethods run the same
check. Its metaclass makes ``isinstance(x, datetime.datetime)`` and
``issubclass`` defer to the ORIGINAL class, so instances of the original (and
of its subclasses, e.g. pandas ``Timestamp``) still pass. ``install`` is
idempotent; :func:`uninstall` restores every original (tests).

The check is by *filename suffix* so it works for an absolute path, a
repo-relative path and a code object compiled with ``compile(src,
genome.__file__, "exec")`` (the F2 exit test).
"""
from __future__ import annotations

import datetime as _datetime_mod
import sys
import time as _time_mod
from typing import Any, Callable, Dict, Optional, Tuple

GUARDED_SUFFIXES: Tuple[str, ...] = (
    "src/factory/genome.py",
    "src/factory/features.py",
)
GUARDED_DIRS: Tuple[str, ...] = ("src/strategies/",)

_ORIG: Dict[str, Any] = {}
_INSTALLED = False


class WallClockError(RuntimeError):
    """Genome / feature / strategy code read the wall clock inside a factory worker."""


def _norm(filename: str) -> str:
    return str(filename).replace("\\", "/")


def is_guarded_filename(filename: str) -> bool:
    """True when ``filename`` belongs to the code that may never read the clock."""
    f = _norm(filename)
    for suf in GUARDED_SUFFIXES:
        if f.endswith(suf):
            return True
    for d in GUARDED_DIRS:
        if f.startswith(d) or ("/" + d) in f:
            return True
    return False


def _check(depth: int, what: str) -> None:
    """Raise when the frame ``depth`` levels above the wrapper is guarded code.

    ``depth`` counts from ``_check`` itself: 0 = ``_check``, 1 = the wrapper,
    2 = the wrapper's caller.
    """
    try:
        frame = sys._getframe(depth)
    except ValueError:  # pragma: no cover - shallower stack than expected
        return
    filename = frame.f_code.co_filename
    if is_guarded_filename(filename):
        raise WallClockError(
            f"{what} called from {_norm(filename)}:{frame.f_lineno} "
            f"({frame.f_code.co_name}); genome/features/strategy code must be a "
            "pure function of the frame (FR-F2.5)"
        )


def _wrap_time(name: str, orig: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _check(2, f"time.{name}")
        return orig(*args, **kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    wrapper.__doc__ = getattr(orig, "__doc__", None)
    wrapper.__wrapped__ = orig  # type: ignore[attr-defined]
    return wrapper


class _GuardMeta(type):
    """``isinstance``/``issubclass`` against the guarded class defer to the original.

    Without this, ``isinstance(pd.Timestamp(...), datetime.datetime)`` would be
    False after :func:`install` (``Timestamp`` subclasses the ORIGINAL class).
    """

    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, cls.__base_orig__)  # type: ignore[attr-defined]

    def __subclasscheck__(cls, sub: Any) -> bool:
        return issubclass(sub, cls.__base_orig__)  # type: ignore[attr-defined]


def _make_guarded_datetime(base: type) -> type:
    class datetime(base, metaclass=_GuardMeta):  # noqa: N801 - keeps repr()/pickling names natural
        """``datetime.datetime`` with the FR-F2.5 tripwire on ``now``/``utcnow``/``today``."""

        __slots__ = ()
        __base_orig__ = base

        @classmethod
        def now(cls, tz: Optional[Any] = None):  # type: ignore[override]
            _check(2, "datetime.datetime.now")
            return super().now(tz)

        @classmethod
        def utcnow(cls):  # type: ignore[override]
            _check(2, "datetime.datetime.utcnow")
            return super().utcnow()

        @classmethod
        def today(cls):  # type: ignore[override]
            _check(2, "datetime.datetime.today")
            return super().today()

    datetime.__module__ = "datetime"
    datetime.__qualname__ = "datetime"
    return datetime


def _wrap_classmethod(owner: type, name: str, label: str) -> None:
    orig = getattr(owner, name)
    fn = orig.__func__ if hasattr(orig, "__func__") else orig

    def wrapper(cls: Any, *args: Any, **kwargs: Any) -> Any:
        _check(2, label)
        return fn(cls, *args, **kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    _ORIG[f"{label}"] = (owner, name, orig)
    setattr(owner, name, classmethod(wrapper))


def install() -> None:
    """Install the tripwire (idempotent). Called by every factory worker initializer."""
    global _INSTALLED
    if _INSTALLED:
        return
    # pandas' C extensions subclass datetime.datetime STATICALLY (ABCTimestamp); a
    # first import after the swap fails with "base type 'datetime' is dynamically
    # allocated". Import it first (optional: numpy-only images have no pandas).
    try:
        import pandas as pd  # noqa: F811
    except ImportError:  # pragma: no cover
        pd = None  # type: ignore[assignment]
    for name in ("time", "time_ns", "monotonic", "monotonic_ns"):
        orig = getattr(_time_mod, name, None)
        if orig is None:  # pragma: no cover - platform without the function
            continue
        _ORIG[f"time.{name}"] = orig
        setattr(_time_mod, name, _wrap_time(name, orig))
    _ORIG["datetime.datetime"] = _datetime_mod.datetime
    _datetime_mod.datetime = _make_guarded_datetime(_datetime_mod.datetime)  # type: ignore[misc]
    if pd is not None:
        ts = pd.Timestamp
        for name in ("now", "today", "utcnow"):
            if hasattr(ts, name):
                try:
                    _wrap_classmethod(ts, name, f"pandas.Timestamp.{name}")
                except (AttributeError, TypeError):  # pragma: no cover - immutable type
                    pass
    _INSTALLED = True


def uninstall() -> None:
    """Restore every original (tests). Safe to call when nothing is installed."""
    global _INSTALLED
    if not _INSTALLED:
        return
    for key, val in list(_ORIG.items()):
        if key.startswith("time."):
            setattr(_time_mod, key[len("time."):], val)
        elif key == "datetime.datetime":
            _datetime_mod.datetime = val  # type: ignore[misc]
        elif key.startswith("pandas.Timestamp."):
            owner, name, orig = val
            setattr(owner, name, orig)
    _ORIG.clear()
    _INSTALLED = False


def installed() -> bool:
    return _INSTALLED


__all__ = [
    "GUARDED_DIRS",
    "GUARDED_SUFFIXES",
    "WallClockError",
    "install",
    "installed",
    "is_guarded_filename",
    "uninstall",
]
