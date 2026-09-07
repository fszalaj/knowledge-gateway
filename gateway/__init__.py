"""knowledge-gateway: filesystem/git-native FastMCP knowledge gateway (vault + code-graph + convert)."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("knowledge-gateway")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"  # a real version would be a lie; the metadata is the source
