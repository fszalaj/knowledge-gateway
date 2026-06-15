"""obsidian-gateway: filesystem/git-native FastMCP gateway for Obsidian vaults."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("obsidian-gateway")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.2.0"
