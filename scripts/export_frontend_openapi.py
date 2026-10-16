"""Export only the public OpenAPI contract. Never read business rows or write settings."""

import json
from pathlib import Path

from huipi_cloud.main import app

if "/api/v1/health" not in app.openapi()["paths"]:
    raise RuntimeError("Frontend contract requires the documented /api/v1 prefix")

path = Path(__file__).resolve().parents[1] / "frontend" / ".cache" / "openapi.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(app.openapi(), ensure_ascii=False), encoding="utf-8")
print("Exported public OpenAPI to frontend/.cache/openapi.json")
