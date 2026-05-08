"""Unit tests for critical code paths: retention, invoices, quiet hours, db_retry, cron."""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
