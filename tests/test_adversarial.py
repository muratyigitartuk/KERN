"""Adversarial / negative tests: malformed input, edge cases, large data."""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from unittest.mock import MagicMock

import pytest
