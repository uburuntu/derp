"""Import every model module so ``Base.metadata`` describes the full schema.

Alembic autogenerate and the schema-drift test read this; add new model
modules here when a package gains tables.
"""

from derp.agent import models as _agent  # noqa: F401
from derp.db.base import Base
from derp.history import models as _history  # noqa: F401
from derp.inference import models as _inference  # noqa: F401
from derp.memory import models as _memory  # noqa: F401
from derp.money import models as _money  # noqa: F401

metadata = Base.metadata
