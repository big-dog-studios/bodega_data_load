"""DB helper for the read API.

Same Cloud SQL Python Connector + SQLAlchemy + pg8000 pattern the loaders use,
but built ONCE at import (this runs as a warm Cloud Run Service, not a per-run
Job). Vendored here because the build context is `./api` — no cross-folder import.

Env: INSTANCE (project:region:instance), DB_NAME, DB_USER, DB_PASS.
A read-only DB user is preferred (set via DB_USER/DB_PASS; no code change).
"""
import os

import sqlalchemy
from google.cloud.sql.connector import Connector

# Keep the Connector alive for the process lifetime (Service stays warm).
_connector = Connector()


def _getconn():
    return _connector.connect(
        os.environ["INSTANCE"], "pg8000",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        db=os.environ["DB_NAME"],
    )


# pool_pre_ping recycles connections dropped while the service was idle.
engine = sqlalchemy.create_engine(
    "postgresql+pg8000://", creator=_getconn, pool_pre_ping=True
)
