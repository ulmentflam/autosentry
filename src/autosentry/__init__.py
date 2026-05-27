"""autosentry — self-healing supervisor for long-running processes."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("autosentry")
except PackageNotFoundError:
    # Running from a source tree without an installed dist (rare — usually
    # only happens in CI for some build steps). Fall back to a sentinel
    # so `autosentry --version` still works.
    __version__ = "0.0.0+unknown"
