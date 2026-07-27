FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime
EXPOSE 8080
CMD gunicorn --chdir dashboard -b 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 180 --access-logfile - --error-logfile - server:app
