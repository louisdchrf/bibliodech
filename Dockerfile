FROM python:3.12-slim

WORKDIR /app

# System deps: Tesseract OCR + français
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY startup.sh ./
COPY docs/ ./docs/
COPY README.md ./
COPY ROADMAP.md ./
RUN chmod +x startup.sh

# Create data directories
RUN mkdir -p /app/data/covers

EXPOSE 8000

CMD ["./startup.sh"]
