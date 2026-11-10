"""Auth/session adapters mapping the ported routers onto OpenData's boundary.

AFI's routers depended on ``app.api.data.deps`` (JWT user + a data-admin
check) and ``app.db.database.get_db``. OpenData already owns equivalent
dependencies, so this module is the single place that translates the two
names rather than scattering the substitution across every router.
"""

from __future__ import annotations

from opendata.api.dependencies import get_current_admin_user, get_current_user
from opendata.core.database import get_db

# Any authenticated, active user may read the data-management plane.
get_current_db_user = get_current_user
# Writes require the OpenData admin role (AFI's data-admin maps onto it).
require_data_admin_user = get_current_admin_user

__all__ = ["get_current_db_user", "require_data_admin_user", "get_db"]
