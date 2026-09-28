FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir .

COPY .env* ./
RUN mkdir -p /app/data && useradd --create-home --shell /usr/sbin/nologin app && chown -R app /app
USER app

CMD ["python", "-m", "app.main"]
