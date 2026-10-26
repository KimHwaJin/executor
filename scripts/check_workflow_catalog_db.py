"""Read-only Agent catalog preflight. Never print connection info or row IDs."""
import json
import os
import sys

import psycopg


def main():
    url = os.environ.get("WORKFLOW_DATABASE_URL") or os.environ.get("EW_DATABASE_URL")
    if not url:
        print("Agent DB environment variable is required; no connection attempted.")
        return 1
    try:
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=5) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            result = {"server_version": conn.execute("SHOW server_version").fetchone()[0]}
            if conn.execute("SELECT to_regclass('ew_alembic_version')").fetchone()[0]:
                result["revision"] = conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0]
            result["vector"] = conn.execute("SELECT default_version, installed_version FROM pg_available_extensions WHERE name='vector'").fetchall()
            for table in ("workflow_catalog", "workflow_version", "workflow_embeddings", "workflow_catalog_requests", "workflow_executions", "workflow_adaptive_history"):
                exists = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
                if not exists:
                    result[table] = {"exists": False}
                    continue
                # Table identifiers come exclusively from the fixed allowlist.
                result[table] = {"rows": conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]}
                result[table]["columns"] = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (table,))]
                result[table]["referencing_fks"] = conn.execute("SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype='f' AND confrelid=to_regclass(%s)", (table,)).fetchall()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        # Driver errors can include host/user/connection strings; print only type.
        print(f"Read-only DB inspection failed ({type(exc).__name__}); connection details suppressed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
