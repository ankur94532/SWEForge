"""Harmless example registered tool; JSON arguments arrive on standard input."""

import json
import sys
from pathlib import Path


def main() -> int:
    arguments = json.load(sys.stdin)
    path = Path(arguments["config_path"])
    if path.is_absolute() or ".." in path.parts:
        print(json.dumps({"valid": False, "error": "config_path must be relative"}))
        return 2
    document = json.loads(path.read_text(encoding="utf-8"))
    checks = document.get("checks")
    valid = isinstance(checks, list) and all(isinstance(item, str) for item in checks)
    print(json.dumps({"valid": valid, "check_count": len(checks or [])}))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
