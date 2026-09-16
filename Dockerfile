# ── Stage: Production Image ──────────────────────────────────
FROM python:3.12-slim-bookworm

LABEL maintainer="JobForge" \
      description="JobForge — Automated Job Application Pipeline"

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── System Dependencies ─────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    perl \
    fontconfig \
    git \
    wget \
    gnupg \
    ca-certificates \
    xz-utils \
    # Playwright Chromium dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# ── Install TinyTeX ──────────────────────────────────────────
ENV PATH="/root/.TinyTeX/bin/x86_64-linux:/root/bin:${PATH}"
RUN wget -qO- "https://yihui.org/tinytex/install-bin-unix.sh" | sh \
    && tlmgr path add \
    && tlmgr install \
        collection-latexrecommended \
        collection-fontsrecommended \
        collection-latexextra \
        enumitem \
        paracol \
        fontawesome5 \
        roboto \
        lato \
        tcolorbox \
        environ \
        trimspaces \
        changepage \
    || true \
    && tlmgr path add

# Ensure TinyTeX is on PATH
ENV PATH="/root/.TinyTeX/bin/x86_64-linux:/root/.TinyTeX/bin/aarch64-linux:${PATH}"

# ── Python Dependencies ─────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Install Playwright Chromium ──────────────────────────────
RUN playwright install chromium

# ── Copy Application Source ──────────────────────────────────
COPY ./src /workspace/src

# ── Expose Port ──────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ───────────────────────────────────────────────
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
