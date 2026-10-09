FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY lens ./lens
RUN pip install --no-cache-dir ".[postgres]" && useradd --system --uid 10001 lens && mkdir /data && chown lens /data
USER lens
# SQLite in a volume by default; point LENS_DATABASE_URL at Postgres for anything shared.
ENV LENS_DATABASE_URL=sqlite:////data/lens.db
VOLUME /data
EXPOSE 8900
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/healthz')"
CMD ["lens", "serve"]
