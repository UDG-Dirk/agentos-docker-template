FROM agnohq/python:3.12

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# ---------------------------------------------------------------------------
# Create non-root user
# ---------------------------------------------------------------------------
RUN groupadd -g 61000 app \
    && useradd -g 61000 -u 61000 -ms /bin/bash app

# ---------------------------------------------------------------------------
# Node.js 22 + Figma MCP (for the HELIX Figma Extractor workflow).
# Installed as root, before `USER app`, and pinned so the agent's
# `npx figma-developer-mcp --stdio` resolves offline (no runtime npm pull).
# Placed early so this layer stays cached across app-code changes.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g figma-developer-mcp@0.13.2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt ./
RUN uv pip sync requirements.txt --system

COPY --chown=app:app . .

# ---------------------------------------------------------------------------
# Persistent baseline volume (Coolify mounts a named volume at /var/lib/helix).
# Create the tree owned by the non-root app user BEFORE `USER app`: an empty
# named volume inherits the image mount-point's ownership on first mount, so
# Agent 3a can clone/fetch the helix-code baseline as uid 61000.
# ---------------------------------------------------------------------------
RUN mkdir -p /var/lib/helix/baseline /var/lib/helix/inventory \
    && chown -R app:app /var/lib/helix

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
RUN chmod +x /app/scripts/entrypoint.sh

USER app

EXPOSE 8000

# Container health signal (CKV_DOCKER_2). curl is installed above; /health needs no auth in any
# RUNTIME_ENV. start-period covers app boot; Coolify/Traefik also probe externally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
