FROM ghcr.io/getpaseo/paseo:latest

USER root

# Install Python 3
RUN apt-get update && apt-get install -y --no-install-recommends python3 && \
    rm -rf /var/lib/apt/lists/*

# Install OMP (Oh My Pi CLI) - pre-downloaded binary
COPY omp-linux-x64 /usr/local/bin/omp
RUN chmod +x /usr/local/bin/omp

# Install code-server - pre-downloaded deb
COPY code-server.deb /tmp/code-server.deb
RUN dpkg -i /tmp/code-server.deb || apt-get update && apt-get install -f -y && dpkg -i /tmp/code-server.deb && \
    rm /tmp/code-server.deb

# Install uv - pre-downloaded tarball
COPY uv.tar.gz /tmp/uv.tar.gz
RUN tar xzf /tmp/uv.tar.gz -C /tmp && \
    cp /tmp/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv && \
    cp /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx && \
    rm -rf /tmp/uv.tar.gz /tmp/uv-x86_64-unknown-linux-gnu

# code-server config
RUN mkdir -p /home/paseo/.config/code-server && \
    echo "bind-addr: 0.0.0.0:8080\nauth: none\ncert: false" > /home/paseo/.config/code-server/config.yaml && \
    chown -R 1000:1000 /home/paseo

# Ensure /workspace exists
RUN mkdir -p /workspace && chown 1000:1000 /workspace

# Install workspace-agent
COPY workspace-agent.py /usr/local/bin/workspace-agent
RUN chmod +x /usr/local/bin/workspace-agent

# Install entrypoint wrapper
COPY entrypoint-wrapper.sh /usr/local/bin/workspace-entrypoint
RUN chmod +x /usr/local/bin/workspace-entrypoint

ENTRYPOINT ["/usr/local/bin/workspace-entrypoint"]

# Install Agent Canvas and Claude Code CLI
RUN npm config set ignore-scripts true && npm install -g @openhands/agent-canvas@1.6.1 @agentclientprotocol/claude-agent-acp@latest
