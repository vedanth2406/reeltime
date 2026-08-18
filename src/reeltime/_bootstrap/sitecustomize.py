"""Injected by ``tape run`` so an unmodified script records itself.

``tape run`` puts this directory at the front of ``PYTHONPATH``. Python's
``site`` module imports ``sitecustomize`` automatically at interpreter startup,
which is early enough to patch httpx before the agent imports anything -- the
whole point of design principle 1, zero-edit adoption.

Shadowing a name the user might already be using is rude, so if they have their
own ``sitecustomize`` it is imported first, with this directory removed from
the path so the real one is found.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

_saved_path = list(sys.path)
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.modules.pop("sitecustomize", None)
try:
    import sitecustomize  # noqa: F401  (the user's own, if any)
except ImportError:
    pass
except Exception as exc:  # pragma: no cover - their code, their problem
    sys.stderr.write("reeltime: your sitecustomize raised: {}\n".format(exc))
finally:
    sys.path = _saved_path

if os.environ.get("REELTIME_AUTOINSTALL"):
    try:
        import reeltime  # noqa: F401  (installs itself; see reeltime/__init__)
    except Exception as exc:  # pragma: no cover - never break the child
        sys.stderr.write("reeltime: could not start recording: {}\n".format(exc))
