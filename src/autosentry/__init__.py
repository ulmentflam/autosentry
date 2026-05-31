"""autosentry — self-healing supervisor for long-running processes."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("autosentry") or "0.0.0+unknown"
except PackageNotFoundError:
    # Running from a source tree without an installed dist (rare — usually
    # only happens in CI for some build steps). Fall back to a sentinel
    # so `autosentry --version` still works. Python 3.13 also returns
    # ``None`` from ``version()`` for partially-broken installs (missing
    # ``RECORD`` file), which is why we coalesce above as well.
    __version__ = "0.0.0+unknown"
