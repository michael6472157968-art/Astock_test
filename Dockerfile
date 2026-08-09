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

# Backup seed configs so entrypoint can restore them after volume mount
RUN mkdir -p /app/data_seed && cp /app/data/factor_weights.json /app/data_seed/ 2>/dev/null || true

# Entrypoint generates .env from env vars so pydantic-settings can read it
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
