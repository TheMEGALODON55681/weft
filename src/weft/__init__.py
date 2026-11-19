import warnings
from importlib.metadata import PackageNotFoundError, version

# A transitive import warns about the optional PySocks extra on every startup. Weft never
# uses SOCKS proxies, and stderr is where an operator reads our logs.
warnings.filterwarnings("ignore", message="SOCKS support in urllib3")

try:
    __version__ = version("weft")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
