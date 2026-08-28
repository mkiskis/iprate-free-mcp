FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IPRATE_MCP_HOST=0.0.0.0 \
    IPRATE_MCP_PORT=8000 \
    IPRATE_MCP_ASSET_ROOT=/data/v1 \
    IPRATE_MCP_ASSET_BASE_URL=https://iprate.eu/data/v1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[builder]" \
    && addgroup --system iprate \
    && adduser --system --ingroup iprate --no-create-home iprate

USER iprate
EXPOSE 8000

CMD ["iprate-free-mcp"]
