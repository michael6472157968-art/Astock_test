#!/bin/bash
set -e

# Generate .env from environment variables if not present.
# pydantic-settings reads .env as a source; env vars alone work too,
# but this ensures compatibility regardless of pydantic-settings version.
if [ ! -f .env ]; then
    cat > .env <<EOF
TUSHARE_TOKEN=${TUSHARE_TOKEN}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
DEBUG=${DEBUG:-false}
CORS_ORIGINS=${CORS_ORIGINS:-*}
ADMIN_SEED_PHONE=${ADMIN_SEED_PHONE:-}
ADMIN_SEED_PASSWORD=${ADMIN_SEED_PASSWORD:-}
EOF
    echo ".env generated from environment variables"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
