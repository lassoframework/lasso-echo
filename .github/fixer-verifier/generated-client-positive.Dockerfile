FROM postgres:17-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/positive-venv \
    && /opt/positive-venv/bin/pip install --no-cache-dir pytest 'psycopg[binary]' Pillow requests boto3 numpy
WORKDIR /workspace
COPY src/ ./
ENV PATH=/opt/positive-venv/bin:/usr/lib/postgresql/17/bin:/usr/bin:/bin \
    PG17_BIN=/usr/lib/postgresql/17/bin PYTHONDONTWRITEBYTECODE=1
USER postgres
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-x", "tests/test_generated_client_linux_positive_seed_pg.py", "tests/test_generated_client_linux_positive_cli.py"]
