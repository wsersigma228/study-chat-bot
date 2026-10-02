FROM docker.io/library/python:3.12-slim

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip check \
    && python -m pip uninstall --yes pip

COPY alembic.ini .
COPY app app
COPY config config
COPY migrations migrations
COPY examples examples
COPY tests tests
COPY pyproject.toml .

CMD ["python", "-m", "app.bot"]
