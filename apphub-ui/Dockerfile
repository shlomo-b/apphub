FROM python:3.12-slim

WORKDIR /app

# for CI github actionss
COPY apphub-ui/requirements.txt .

# for local testing
#COPY requirements.txt .

# hadolint ignore=DL3005,DL3008
RUN apt-get update -y \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        pkg-config \
        libxml2-dev \
        libxslt1-dev \
        libxmlsec1-dev \
        libxmlsec1-openssl \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

# for CI github actions
COPY apphub-ui/app ./app
COPY apphub-ui/static ./static

# for local testing
# COPY app ./app
# COPY static ./static
RUN mkdir -p /app/data

# ------------ NEW (security best practice) -------------
# Create a non-root user and give it the app files
RUN adduser --disabled-password --gecos "" appuser --uid 1000 \
    && chown -R appuser:appuser /app

# Switch to the new user
USER appuser

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
