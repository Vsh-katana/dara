FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINANCE_SEED_DEFAULT=0 \
    DARA_DATA_DIR=/var/lib/dara
RUN groupadd --gid 10001 dara \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dara \
 && mkdir -p /opt/dara /var/lib/dara \
 && chown -R 10001:10001 /opt/dara /var/lib/dara
WORKDIR /opt/dara
COPY requirements.txt ./requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
      --retries 2 --timeout 20 --index-url "${PIP_INDEX_URL}" \
      -r requirements.txt
COPY --chown=10001:10001 backend ./backend
COPY --chown=10001:10001 frontend ./frontend
USER 10001:10001
EXPOSE 8765
CMD ["uvicorn", "backend.server_main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]

