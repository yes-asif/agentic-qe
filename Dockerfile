FROM python:3.11-slim

# Node.js + npx are required to spawn the official @playwright/mcp server subprocess
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-fetch the Playwright MCP server package + browser binaries at build time
RUN npx -y @playwright/mcp@latest --version || true
RUN npx -y playwright install --with-deps chromium

COPY app ./app
COPY configs ./configs

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
