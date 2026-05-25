FROM python:3.12-slim

# Install OS deps needed by bcrypt's C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Runtime defaults — override via k8s Secret/ConfigMap
ENV FLASK_APP=wsgi.py \
    FLASK_ENV=production \
    DATABASE_URL=sqlite:////app/instance/database/chores.db

EXPOSE 8000

# Single worker prevents APScheduler from running in multiple processes.
# --threads 4 handles concurrent requests within that one worker.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:app"]
