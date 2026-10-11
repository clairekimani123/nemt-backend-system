FROM python:3.12-slim
WORKDIR /code
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"
