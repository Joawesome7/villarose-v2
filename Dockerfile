# Use a lightweight Python base image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system deps (needed for psycopg2)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install gunicorn if not in requirements.txt
RUN pip install gunicorn

# Copy the rest of your project files
COPY . .

# Prevent Python buffering (helps with Railway logs)
ENV PYTHONUNBUFFERED=1

# Railway dynamically assigns $PORT — your app must listen on it
# No need to EXPOSE; Railway handles routing

# Use gunicorn for production
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT app:app"]