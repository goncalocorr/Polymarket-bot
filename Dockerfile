FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data and logs dirs + non-root user
RUN mkdir -p data logs docs/data && \
    adduser --disabled-password --gecos "" botuser && \
    chown -R botuser:botuser /app

USER botuser

CMD ["python", "main.py"]
