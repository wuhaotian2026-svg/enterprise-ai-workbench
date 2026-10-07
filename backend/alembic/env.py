from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from policy_api.approvals import models as _approval_models  # noqa: F401
from policy_api.assistant_drafts import models as _assistant_draft_models  # noqa: F401
from policy_api.hr import models as _hr_models  # noqa: F401
from policy_api.models import Base
from policy_api.procurement import models as _procurement_models  # noqa: F401
from policy_api.slot_extraction import models as _slot_extraction_models  # noqa: F401
from policy_api.tools import models as _tool_models  # noqa: F401
from policy_api.workbench import audit as _workbench_audit  # noqa: F401
from policy_api.workbench import capabilities as _workbench_capabilities  # noqa: F401
from policy_api.workbench import events as _workbench_events  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section) or {}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
