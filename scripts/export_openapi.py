import json
from pathlib import Path
from app.main import app


if __name__ == "__main__":
    Path("docs/openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("OpenAPI contract exported")
