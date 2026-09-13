FROM python:3.12-slim

# Set INSTALL_LIBREOFFICE=true to accept legacy .ppt uploads (adds ~400 MB to the image).
ARG INSTALL_LIBREOFFICE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    PORT=8000 \
    UPLOAD_DIR=/tmp/ten-iqs-uploads

RUN if [ "$INSTALL_LIBREOFFICE" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libreoffice-impress && \
      rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"

# The app reads PORT itself (one worker; sessions are held in process memory), so the command
# works in exec form on any platform - including Railway, which runs start commands without a shell.
CMD ["python", "-m", "app.main"]
