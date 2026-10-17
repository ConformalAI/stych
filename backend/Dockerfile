# Multi-stage Docker build for Stych backend

# Stage 1: Base image with dependencies
FROM python:3.11-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy project (needed for pip install -e .)
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Stage 2: Production image
FROM python:3.11-slim

WORKDIR /app

# Install runtime deps + Node.js (required for MCP stdio integrations via npx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from base
COPY --from=base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create data directories (cleaning sandboxes live outside /app — see CLEANING_WORKSPACE_BASE)
RUN mkdir -p /app/data/uploads /app/data/temp /app/logs \
    /data/cleaning_workspaces /data/workspaces

# Create non-root user
RUN useradd -m -u 1000 stych && \
    chown -R stych:stych /app /data/cleaning_workspaces /data/workspaces

USER stych

ENV CLEANING_WORKSPACE_BASE=/data/cleaning_workspaces

# Expose ports
EXPOSE 8000 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Run database migrations, then start the application.
# (Invoked through `sh` so the script needs no executable bit.)
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
