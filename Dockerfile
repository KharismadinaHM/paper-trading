FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Jalankan aplikasi sebagai user non-root
RUN useradd --create-home --uid 10001 app && mkdir -p /app/logs && chown -R app:app /app

# Copy application source code
COPY --chown=app:app app/ ./app/
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations/ ./migrations/

USER app

# Expose FastAPI Dashboard port
EXPOSE 8000

# Default command: start FastAPI dashboard
CMD ["uvicorn", "app.dashboard:app", "--host", "0.0.0.0", "--port", "8000"]
