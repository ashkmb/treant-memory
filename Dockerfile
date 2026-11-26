# python:3.13-slim (tag), pinned by digest
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# ghcr.io/astral-sh/uv:0.13.3 (tag), pinned by digest (multi-arch index)
COPY --from=ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1 /uv /usr/local/bin/uv

ARG UID=10001
ARG GID=10001
RUN groupadd --gid ${GID} treant \
    && useradd --uid ${UID} --gid ${GID} --no-create-home --shell /usr/sbin/nologin treant

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

USER treant
EXPOSE 8000
# Ownership check for the MCP registry (registry.modelcontextprotocol.io).
LABEL io.modelcontextprotocol.server.name="io.github.ashkmb/treant-memory"
# TCP connect to the port in TREANT_BIND (host:port, default 127.0.0.1:8000).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('TREANT_BIND', '').rpartition(':')[2] or 8000)), 3).close()"]

ENTRYPOINT ["treant"]
CMD ["serve"]
