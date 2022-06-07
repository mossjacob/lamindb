"""lamindb: Manage files & data.

Import the package::

   import lamindb as lndb

Main functionality:

.. autosummary::
   :toctree: .

   ingest
   DB

Settings:

.. autosummary::
   :toctree: .

   settings
"""

__version__ = "0.1.1"
from . import storage  # noqa
from ._db._notion import Dataset  # noqa
from ._db._sqlite import DB  # noqa
from ._ingest import ingest  # noqa
from ._logging import logger  # noqa
from ._settings import settings  # noqa
