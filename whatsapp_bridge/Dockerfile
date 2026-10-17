# ==============================================================================
# BOTGI WHATSAPP BRIDGE - DOCKERFILE
# ==============================================================================
FROM node:20-bookworm-slim

# Set environment variables
ENV NODE_ENV=production \
    WA_HEADLESS=true \
    WA_BRIDGE_PORT=3001 \
    WA_AUTH_ROOT=/data/auth

# Install Chromium and headless browser system dependencies for Puppeteer
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgcc1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency manifests
COPY package*.json ./

# Install npm dependencies
RUN npm ci --only=production || npm install --production

# Copy application code
COPY . .

# Create directory for persistent WhatsApp session data
RUN mkdir -p /data/auth

# Expose bridge port
EXPOSE 3001

# Start the bridge service
CMD ["node", "server.js"]
