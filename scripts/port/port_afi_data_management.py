#!/usr/bin/env python3
"""Inventory AFI's data-management code and map it onto OpenData for the port.

Owner ruling (2026-11-09): port first, then delete from AFI. This tool is stage 0 of that
port. It does not move anything unless ``--apply`` is passed; on its own it produces the
manifest that every later batch is executed and reviewed against.

Why a namespace (``afi_port``) instead of writing over OpenData's own plane: OpenData already
has ``opendata/api/{tasks,scripts,tables,executions,interfaces}`` and
``opendata/models/{task,data_script,data_table,interface}``. Copying AFI's implementation on
top of those would silently replace a working (if empty) plane with an unverified one. The
port lands beside it, is adapted and tested there, and only then replaces it — after which the
AFI copies can be deleted under the owner's ruling.

Classification:
  copy        the file has no OpenData counterpart; a mechanical copy works
  rewrite     same, but ``app.*`` imports must be rewritten to reach the ported modules
  adapt       OpenData already owns an equivalent module; a human must reconcile them
  keep-in-afi parts that must stay in AFI until OpenData actually runs the scheduler
              (listed so "delete from AFI" is not mistaken for "delete today")
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil

AFI_BACKEND_API = "src/backend/app/api/akshare"
AFI_BACKEND_SERVICES = "src/backend/app/services/akshare"
AFI_BACKEND_MODEL = "src/backend/app/models/akshare_mgmt.py"
AFI_FRONTEND_VIEWS = "src/frontend/src/views/data"
AFI_FRONTEND_API = ("src/frontend/src/api/akshare.ts", "src/frontend/src/api/dataGovernance.ts")

# Modules whose removal from AFI stops the live collection plane: the scheduler and the
# worker that execute the 1048 armed tasks. They stay until OpenData runs them.
KEEP_IN_AFI_UNTIL_CUTOVER = {
    "src/backend/app/services/akshare/scheduler.py",
    "src/backend/app/services/akshare/scheduler_service.py",
    "src/backend/app/services/akshare/script_worker.py",
    "src/backend/app/services/akshare/script_worker_runtime.py",
    "src/backend/app/services/akshare/run_coordinator.py",
    "src/backend/app/services/akshare/write_fence.py",
    "src/backend/app/services/akshare/execution.py",
    "src/backend/app/services/akshare/clock.py",
}

# Files that already have an OpenData module doing the same job; these need adaptation,
# not a copy.
OPENDATA_COUNTERPARTS = {
    "src/backend/app/api/akshare/scripts.py": "opendata/api/scripts.py",
    "src/backend/app/api/akshare/tasks.py": "opendata/api/tasks.py",
    "src/backend/app/api/akshare/executions.py": "opendata/api/executions.py",
    "src/backend/app/api/akshare/interfaces.py": "opendata/api/interfaces.py",
    "src/backend/app/api/akshare/tables.py": "opendata/api/data.py",
    "src/backend/app/services/akshare/script.py": "opendata/services/script_service.py",
    "src/backend/app/services/akshare/interface.py": "opendata/services/interface_service.py",
    "src/backend/app/models/akshare_mgmt.py": "opendata/models/{data_script,data_table,task,interface}.py",
}

BACKEND_REWRITES = (
    ("from app.data_fetch.configs.db_config import", "from opendata.afi_port.data_fetch.db_config import"),
    ("from app.data_fetch.providers.akshare_to_mysql import", "from opendata.afi_port.data_fetch.akshare_to_mysql import"),
    ("from app.data_fetch.core.mysql_base import", "from opendata.afi_port.data_fetch.mysql_base import"),
    ("from app.services.akshare.", "from opendata.afi_port.services."),
    ("from app.models.akshare_mgmt import", "from opendata.afi_port.models.akshare_mgmt import"),
    ("from app.api.akshare.", "from opendata.afi_port.api."),
)


def target_for(relative: str) -> str:
    if relative.startswith(AFI_BACKEND_API):
        return relative.replace("src/backend/app/api/akshare", "opendata/afi_port/api")
    if relative.startswith(AFI_BACKEND_SERVICES):
        return relative.replace("src/backend/app/services/akshare", "opendata/afi_port/services")
    if relative == AFI_BACKEND_MODEL:
        return "opendata/afi_port/models/akshare_mgmt.py"
    if relative.startswith(AFI_FRONTEND_VIEWS):
        return relative.replace("src/frontend/src/views/data", "frontend/src/views/afi-data")
    if relative in AFI_FRONTEND_API:
        return "frontend/src/api/afi-data/" + pathlib.Path(relative).name
    return "opendata/afi_port/misc/" + pathlib.Path(relative).name


def sources(afi: pathlib.Path) -> list[str]:
    found: list[str] = []
    for directory, suffix in ((AFI_BACKEND_API, ".py"), (AFI_BACKEND_SERVICES, ".py")):
        base = afi / directory
        if base.is_dir():
            found += [str(p.relative_to(afi)) for p in sorted(base.glob(f"*{suffix}")) if p.name != "__init__.py"]
    if (afi / AFI_BACKEND_MODEL).is_file():
        found.append(AFI_BACKEND_MODEL)
    views = afi / AFI_FRONTEND_VIEWS
    if views.is_dir():
        found += [str(p.relative_to(afi)) for p in sorted(views.glob("*.vue"))]
    found += [rel for rel in AFI_FRONTEND_API if (afi / rel).is_file()]
    return found


def classify(relative: str) -> str:
    if relative in KEEP_IN_AFI_UNTIL_CUTOVER:
        return "keep-in-afi"
    if relative in OPENDATA_COUNTERPARTS:
        return "adapt"
    return "rewrite" if relative.endswith(".py") else "copy"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--afi-root", required=True)
    parser.add_argument("--opendata-root", required=True)
    parser.add_argument("--out", default="-")
    parser.add_argument("--apply", action="store_true", help="actually copy the files")
    args = parser.parse_args()

    afi = pathlib.Path(args.afi_root).resolve()
    opendata = pathlib.Path(args.opendata_root).resolve()
    rows = []
    for relative in sources(afi):
        source = afi / relative
        target = target_for(relative)
        row = {
            "source": relative,
            "target": target,
            "classification": classify(relative),
            "lines": len(source.read_text(encoding="utf-8", errors="replace").splitlines()),
            "counterpart": OPENDATA_COUNTERPARTS.get(relative),
            "target_exists": (opendata / target).exists(),
        }
        rows.append(row)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1

    if args.apply:
        for row in rows:
            if row["classification"] == "keep-in-afi":
                continue
            destination = opendata / row["target"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = (afi / row["source"]).read_text(encoding="utf-8", errors="replace")
            if row["classification"] == "rewrite":
                for old, new in BACKEND_REWRITES:
                    text = text.replace(old, new)
            destination.write_text(text, encoding="utf-8")

    document = {
        "afi_root": str(afi),
        "opendata_root": str(opendata),
        "applied": args.apply,
        "files": len(rows),
        "lines": sum(row["lines"] for row in rows),
        "classification_counts": counts,
        "rows": rows,
    }
    text = json.dumps(document, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(text)
    else:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(
            f"files={document['files']} lines={document['lines']} "
            f"classes={counts} applied={args.apply} -> {args.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
