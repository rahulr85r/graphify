"""Turn a relational database into a graph you can actually read."""

__version__ = "0.1.0"

from .ingest import open_source            # noqa: F401
from .infer import infer, GraphModel       # noqa: F401
from .graph import build_index, expand     # noqa: F401
