FROM python:3.10-slim

# Install system build tools and OpenCV/Dlib runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    pkg-config \
    g++ \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip & install build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

# Create required directories
RUN mkdir -p uploads/faces temp_uploads instance

# Expose both 8080 (Runsite platform standard) and 7860 (Hugging Face fallback)
EXPOSE 8080 7860

ENV PORT=8080
ENV HOST=0.0.0.0

# Memory-safe Gunicorn footprint (~39MB RAM) optimized for 256MB free plan limits
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --access-logfile - --error-logfile - --workers 1 --threads 2 --timeout 120 app:app"]
