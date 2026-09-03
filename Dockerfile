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

# Copy application source code
COPY app/ ./app/
COPY .env.example .

# Expose FastAPI Dashboard port
EXPOSE 8000

# Default command: start FastAPI dashboard
CMD ["uvicorn", "app.dashboard:app", "--host", "0.0.0.0", "--port", "8000"]
