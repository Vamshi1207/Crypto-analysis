FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Official installer drops the binary in ~/.local/bin
    PATH="/root/.local/bin:${PATH}"

# Antigravity CLI (agy) replaced the deprecated Gemini CLI.
# Native binary install — no Node required. Docs:
# https://antigravity.google/docs/cli/install/
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && ln -sf /root/.local/bin/agy /usr/local/bin/agy \
    && agy --version \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sleep", "infinity"]
