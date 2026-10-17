# ==============================================================================
# BOTGI CRM & BILLING PLATFORM - DOCKERFILE
# ==============================================================================
FROM python:3.12-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Set working directory
WORKDIR /app

# Install system build dependencies and runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install Python requirements
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . /app/

# Ensure the entrypoint has Unix line endings (CRLF -> LF) and remains executable
RUN sed -i 's/\r$//' /app/entrypoint.sh && \
    chmod 755 /app/entrypoint.sh && \
    mkdir -p /app/media /app/staticfiles

# Expose default application port
EXPOSE 8000

# Set entrypoint and default command
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "botgi_crm.asgi:application"]
