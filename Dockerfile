FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + system deps
RUN playwright install --with-deps chromium

# Pre-download embedding model (cached in /root/.cache)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy source
COPY src/ ./src/
COPY main.py .

# Expose port for Feishu callback server
EXPOSE 8080

# Default: run background scheduler
CMD ["python", "main.py", "run"]
