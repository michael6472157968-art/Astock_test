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
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}
DEBUG=${DEBUG:-false}
CORS_ORIGINS=${CORS_ORIGINS:-*}
ADMIN_SEED_PHONE=${ADMIN_SEED_PHONE:-}
ADMIN_SEED_PASSWORD=${ADMIN_SEED_PASSWORD:-}
EOF
    cp .env "$PERSIST_ENV"
    echo ".env generated and persisted to volume"
fi

# Seed config files into the mounted volume（总是覆盖，保证 git 里的配置更新能同步到线上）
# (/app/data is a Fly mount — it hides Docker-image files at the same path)
for cfg in factor_weights.json factor_meta.json risk_signals.json eye_weights.json; do
    if [ -f "/app/data_seed/$cfg" ]; then
        cp "/app/data_seed/$cfg" "/app/data/$cfg"
        echo "Seeded /app/data/$cfg from image"
    fi
done

# Run alembic migrations before starting the app
if [ -f alembic.ini ]; then
    echo "Running database migrations..."
    alembic upgrade head || echo "Migration warning (non-fatal)"
fi

# Pre-flight: check if app can be imported before starting uvicorn
echo "Testing app import..."
python -c "from app.main import app; print('App import OK')" || {
    echo "APP IMPORT FAILED — see traceback above"
    exit 3
}

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
