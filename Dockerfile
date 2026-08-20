FROM python:3.12-slim

# Set timezone to China Standard Time
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install Python dependencies (all have pre-built wheels, no compiler needed)
COPY backend/requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# Copy application code
COPY backend/ /app/
COPY frontend/ /frontend/

# Ensure data directory exists (volume mount point for SQLite)
RUN mkdir -p /app/data

# Backup seed configs — /app/data is shadowed by Fly volume mount at runtime,
# so config files shipped in the image must be stashed elsewhere and restored by entrypoint.
RUN mkdir -p /app/data_seed
COPY backend/data/factor_weights.json /app/data_seed/
COPY backend/data/factor_meta.json /app/data_seed/
COPY backend/data/risk_signals.json /app/data_seed/
COPY backend/data/eye_weights.json /app/data_seed/

# Entrypoint generates .env from env vars so pydantic-settings can read it
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
