import os

# test_urls.py is a standalone, network-and-binary manual integration script
# (run directly with `python tests/test_urls.py`), not a pytest module — its
# helper functions take positional args and would mis-collect. Exclude it from
# pytest collection so the default `pytest` run stays fully offline.
collect_ignore = ["test_urls.py"]
