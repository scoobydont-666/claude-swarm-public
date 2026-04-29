#!/usr/bin/env python3
"""Export OpenAPI spec from the claude-swarm dashboard FastAPI app."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dashboard import app  # noqa: E402

spec = app.openapi()
out = Path(__file__).parent.parent / "openapi.json"
out.write_text(json.dumps(spec, indent=2) + "\n")
print(f"Wrote {out} ({len(spec.get('paths', {}))} endpoints)")
