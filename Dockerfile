FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r bitchat && useradd -r -g bitchat -d /var/lib/bitchat-node -s /usr/sbin/nologin bitchat

WORKDIR /opt/bitchat-node

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --quiet --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --quiet --no-cache-dir .

# Create data and config directories
RUN mkdir -p /var/lib/bitchat-node /etc/bitchat-node \
    && chown -R bitchat:bitchat /var/lib/bitchat-node /etc/bitchat-node

# Default config (can be overridden by volume mount)
COPY config.yaml /etc/bitchat-node/config.yaml

USER bitchat

# TCP peer port
EXPOSE 8765
# REST + WebSocket API port
EXPOSE 8080

VOLUME ["/var/lib/bitchat-node", "/etc/bitchat-node"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request, sys; urllib.request.urlopen('http://127.0.0.1:8080/health'); sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "-m", "daemon"]
CMD ["--config", "/etc/bitchat-node/config.yaml"]
