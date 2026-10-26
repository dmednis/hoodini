FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

RUN pip install --no-cache-dir uv==0.10.9
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/pyproject.toml
COPY server/src/ server/src/
COPY LICENSE LICENSE
RUN uv lock --check && uv build --package hoodini --wheel --out-dir /wheels && \
    uv export --locked --no-dev --no-emit-workspace --no-emit-project \
      --format requirements-txt --output-file /requirements.txt > /dev/null && \
    python -m venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --require-hashes -r /requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps /wheels/hoodini-0.1.0-*.whl

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
LABEL org.opencontainers.image.source="https://github.com/dmednis/hoodini" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="Unofficial local hood integration; not affiliated with or endorsed by Elica S.p.A."
RUN groupadd --system --gid 999 hoodini && useradd --system --uid 999 --gid hoodini --home-dir /data hoodini && \
    mkdir /data && chown hoodini:hoodini /data && chmod 700 /data
COPY --from=builder /opt/venv /opt/venv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/opt/venv/bin:$PATH" HOODINI_MODE=standalone HOODINI_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8883 8765
ENTRYPOINT ["/bin/sh", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
