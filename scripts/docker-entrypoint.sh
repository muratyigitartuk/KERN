#!/bin/bash
set -e

# Run database migrations before starting the application
python -c "
from pathlib import Path
from app.config import settings
from app.database import connect
from app.platform import connect_platform_db

# Ensure profile database schema is current
connect(settings.db_path)
# Ensure system/platform database schema is current
connect_platform_db(settings.system_db_path)
print('Database migrations complete.')
"

exec "$@"
