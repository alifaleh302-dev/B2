# Playwright official image — chromium + OS deps are pre-installed
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Install Python deps first (caches well)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . .

# Create runtime directories (also created at runtime as fallback)
RUN mkdir -p sessions logs data

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=10000

EXPOSE 10000

CMD ["python", "main.py"]
