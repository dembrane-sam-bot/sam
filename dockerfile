FROM node:22-slim

# System packages: git for repo work, python for daemon, gh for GitHub API, build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    curl \
    gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
 && apt-get update && apt-get install -y gh \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI, pinned
RUN npm install -g @anthropic-ai/claude-code@2.1.139

# Non-root user
RUN useradd --create-home --shell /bin/bash sam
USER sam
WORKDIR /home/sam

# Python deps
COPY --chown=sam:sam src/runtime/requirements.txt /home/sam/src/runtime/requirements.txt
RUN pip install --user --break-system-packages -r /home/sam/src/runtime/requirements.txt
ENV PATH="/home/sam/.local/bin:${PATH}"

# Sam's source — copied last so code changes don't bust dep cache
COPY --chown=sam:sam . /home/sam/

# Volume for state
VOLUME ["/data"]

CMD ["python3", "-m", "src.runtime.daemon"]