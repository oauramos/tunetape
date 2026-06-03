from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tunetape")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.4.0"
