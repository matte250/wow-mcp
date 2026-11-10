"""One module per feature. Importing this package imports them all, which registers their
tools and guidance (see wow_mcp.app)."""

import importlib
import pkgutil

for _module in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_module.name}")
