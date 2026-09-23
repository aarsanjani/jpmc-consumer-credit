FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GOOGLE_GENAI_USE_VERTEXAI=true

# GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, VERTEX_AGENT_ENGINE_ID,
# BIGQUERY_DATASET and DATAPLEX_GLOSSARY are deliberately NOT baked in. The
# image previously carried a real project's engine id as its default, so a
# container started without --set-env-vars came up healthy and read a stranger's
# Memory Bank. The backend now raises on boot when they are unset.
#
#   gcloud run deploy <service> --image <image> --region us-central1 \
#     --set-env-vars GOOGLE_CLOUD_PROJECT=...,GOOGLE_CLOUD_LOCATION=us-central1,\
# VERTEX_AGENT_ENGINE_ID=...,BIGQUERY_DATASET=...,DATAPLEX_GLOSSARY=...

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY backend/ backend/
COPY frontend/ frontend/
COPY agent/ agent/

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "backend.app:app"]
