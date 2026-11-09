# syntax=docker/dockerfile:1.7
# Autossuficiente: o contexto de build é só este repositório.
# O portfolio-core vem do GitHub, na tag fixada em requirements-core.txt. Para testar com uma cópia local
# (sem publicar o core antes), aponte o estágio "core" para a pasta:
#   docker build --build-context core=../_core -t portfolio-planilhas .
FROM scratch AS core

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
COPY requirements.txt requirements-core.txt ./
RUN pip install -r requirements.txt
RUN --mount=type=bind,from=core,target=/core \
    if [ -f /core/pyproject.toml ]; then cp -r /core /tmp/core && pip install "/tmp/core[langchain]"; \
    else pip install -r requirements-core.txt; fi

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN useradd --system --create-home --uid 10001 app
WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app app ./app
COPY --chown=app web ./web
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
