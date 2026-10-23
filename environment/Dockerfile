FROM python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/ /app/
COPY vendor/ /opt/vendor/
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chmod 0755 /app/analyze.py \
    && mkdir -p /logs/verifier \
    && chown -R appuser:appuser /app /logs/verifier

CMD ["python3", "/app/analyze.py", "--help"]
