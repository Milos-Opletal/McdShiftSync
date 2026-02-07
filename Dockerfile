FROM python:3.11-slim

# Set working directory
WORKDIR /app

# 1. Install system tools required for building Python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies directly
# Versions removed so it installs the latest stable releases
RUN pip install --no-cache-dir \
    protobuf \
    Flask \
    Flask-SQLAlchemy \
    google-api-python-client \
    google-auth \
    google-auth-oauthlib \
    pytz \
    python-dateutil \
    requests \
    playwright \
    gunicorn

# 3. Install Playwright Browsers + System Dependencies
# "chromium" is specified to save space. Remove it to install all browsers.
RUN playwright install --with-deps chromium

# Set environment variables
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
ENV PYTHONUNBUFFERED=1

# 4. Copy application files
COPY . .

# Create db directory
RUN mkdir -p /app/db

# Expose the application port
EXPOSE 4330