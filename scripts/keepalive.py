# SPDX-License-Identifier: AGPL-3.0-only
"""Refresh only a public monthly activity marker."""

import json
from datetime import UTC, datetime
from pathlib import Path

path = Path(".github/activity.json")
value = {"schema_version": 1, "month": datetime.now(UTC).strftime("%Y-%m")}
if not path.exists() or json.loads(path.read_text()) != value:
    path.write_text(json.dumps(value, indent=2) + chr(10))
