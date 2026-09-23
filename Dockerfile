FROM python:3.11-slim

WORKDIR /app

# Only genuinely image-level settings are baked in. Project ids, the Agent
# Engine id, the BigQuery dataset and the Dataplex glossary are deliberately
# NOT set here: an image that carries a real resource id will happily connect
# to that resource from anywhere it is run. Supply them at run time, e.g.
#
#   docker run --env-file .env -p 8080:8080 <image>
#
# `backend/config.py` raises ConfigurationError on startup if any are missing,
# so a misconfigured container fails loudly instead of serving invented data.
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GOOGLE_GENAI_USE_VERTEXAI=true

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY backend/ backend/
COPY frontend/ frontend/
COPY agent/ agent/

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "backend.app:app"]
