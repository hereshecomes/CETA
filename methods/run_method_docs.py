"""run_method.py with the application's public documents replaced (document ablations E11 / E12 / E14).
The wrapper removes `--docs DIR` from the command line, points the application profile's `docs` entry at
DIR (the agents read policy.md / schema.md through app_profile.current()["docs"] at call time), and then
runs run_method.main() unchanged. Nothing else differs: same prompts, schemas, budgets, scoring, output
layout. DIR must contain policy.md and schema.md.

Usage: run_method_docs.py --docs DIR <every argument of run_method.py>
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
sys.path.insert(0, str(ROOT / "evaluation"))

if "--docs" not in sys.argv:
    raise SystemExit("--docs DIR is required")
_i = sys.argv.index("--docs")
DOCS = Path(sys.argv[_i + 1]).resolve()
del sys.argv[_i:_i + 2]
for _f in ("policy.md", "schema.md"):
    if not (DOCS / _f).exists():
        raise SystemExit(f"{DOCS / _f} missing")

import app_profile  # noqa: E402
import run_method  # noqa: E402  (selects the application from --app at import)

app_profile.current()["docs"] = DOCS
print(json.dumps({"docs_override": str(DOCS), "app": app_profile.name()}), flush=True)

if __name__ == "__main__":
    run_method.main()
