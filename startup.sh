#!/bin/sh
set -e

# 1. Migrations legacy (colonnes, backfills) — idempotent
python3 -c "from app.database import init_db; init_db()"

# 2. Si la table alembic_version n'existe pas encore, stamper la DB existante
#    pour dire à Alembic "tu pars d'ici" sans rejouer la baseline
python3 - <<'EOF'
import os
from sqlalchemy import create_engine, text
url = os.environ.get("DATABASE_URL", "sqlite:////app/data/bibliodech.db")
engine = create_engine(url, connect_args={"check_same_thread": False})
with engine.connect() as conn:
    tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
    has_alembic = "alembic_version" in tables
if not has_alembic:
    import subprocess
    subprocess.run(["alembic", "stamp", "head"], check=True)
    print("[startup] DB existante marquée à la version Alembic courante", flush=True)
EOF

# 3. Appliquer les nouvelles migrations (no-op si déjà à jour)
alembic upgrade head

# 4. Lancer l'application
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
