from __future__ import annotations

import os

# All tests run in personal posture — production posture requires a real
# PlatformStore, which integration-style test helpers already provide.
os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")
