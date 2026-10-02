FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY alembic.ini .
COPY app app
COPY config config
COPY migrations migrations
COPY examples examples
COPY tests tests
COPY pyproject.toml .

CMD ["python", "-m", "app.bot"]
