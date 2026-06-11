"""Thin re-export of the shared review-gating contract.

The implementation lives in plugins/air/lib/verdict.py — the SAME file the
CLI's review.md Step 12 executes directly — so the two delivery modes can
never gate the same review body differently (this killed the last
pre-v1.12 CLI gating drift).

Loaded by explicit file path rather than `import verdict`: this module IS
named verdict, and managed/ sits first on sys.path for every managed
import, so a name-based import would resolve right back here. The loaded
module registers under a distinct name in sys.modules so repeated imports
share one instance (the lib's compiled regexes and constants stay
singletons).
"""
import importlib.util
import sys
from pathlib import Path

_LIB_VERDICT = (
    Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib" / "verdict.py"
)
_MODULE_NAME = "air_shared_verdict"

if _MODULE_NAME in sys.modules:
    _mod = sys.modules[_MODULE_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_MODULE_NAME, _LIB_VERDICT)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE_NAME] = _mod
    _spec.loader.exec_module(_mod)

# Re-export everything public plus the leading-underscore internals the
# managed tests exercise directly (dunder/module plumbing excluded).
globals().update(
    {k: v for k, v in vars(_mod).items() if not k.startswith("__")}
)
