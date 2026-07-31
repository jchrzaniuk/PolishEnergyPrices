FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PEP_CONFIG=/config/config.yaml

WORKDIR /app

COPY service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && groupadd --gid 10001 pep \
    && useradd --uid 10001 --gid pep --no-create-home --home-dir /app pep \
    && mkdir -p /config /data \
    && chown -R pep:pep /app /config /data

COPY --chown=pep:pep custom_components /app/custom_components
COPY --chown=pep:pep service /app/service

USER pep

EXPOSE 8080
VOLUME ["/config", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "service"]
