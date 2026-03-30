FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install Python dependencies directly
# Stripped Playwright and python-dateutil as they are obsolete
RUN pip install --no-cache-dir \
    protobuf \
    Flask \
    Flask-SQLAlchemy \
    google-api-python-client \
    google-auth \
    google-auth-oauthlib \
    pytz \
    requests \
    beautifulsoup4 \
    gunicorn

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Copy application files
COPY . .

# Create safely mapped db directory
RUN mkdir -p /app/db

# Expose the application port
EXPOSE 4330