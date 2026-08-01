#!/bin/bash
set -e

PERSIST_ENV="/app/data/.env"

# Restore persisted .env from volume, or generate a new one
if [ -f "$PERSIST_ENV" ]; then
    cp "$PERSIST_ENV" .env
    echo ".env restored from persistent volume"
else
    # Auto-generate JWT secret if not provided via env
    if [ -z "$JWT_SECRET_KEY" ]; then
        JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    fi

    cat > .env <<EOF
TUSHARE_TOKEN=${TUSHARE_TOKEN}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
DEBUG=${DEBUG:-false}
CORS_ORIGINS=${CORS_ORIGINS:-*}
ADMIN_SEED_PHONE=${ADMIN_SEED_PHONE:-}
ADMIN_SEED_PASSWORD=${ADMIN_SEED_PASSWORD:-}
EOF
    cp .env "$PERSIST_ENV"
    echo ".env generated and persisted to volume"
fi

# Run alembic migrations before starting the app
if [ -f alembic.ini ]; then
    echo "Running database migrations..."
    alembic upgrade head || echo "Migration warning (non-fatal)"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
