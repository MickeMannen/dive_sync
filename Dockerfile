FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set workspace
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy sources and utilities
COPY src/ ./src/
COPY setup_credentials.py ./
COPY docker_run.py ./
COPY sync.py ./

# Create default directories for volume mapping
RUN mkdir -p /app/data

# Expose default web server port
EXPOSE 8000

# Environment variables to support user configurations
ENV DATA_DIR=/app/data
ENV DIVE_SYNC_PORT=8000
ENV DIVE_SYNC_HOST=0.0.0.0

# Start FastAPI server
ENTRYPOINT ["python", "docker_run.py"]
