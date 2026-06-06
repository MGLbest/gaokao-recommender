FROM python:3.12-slim

WORKDIR /app

# Install only what's needed
RUN pip install --no-cache-dir flask flask-cors gunicorn

# Copy app code
COPY backend/ backend/

# Copy database
COPY data/gaokao_v4.db data/gaokao_v4.db

# Set DB path for Render compatibility
ENV DB_PATH=/app/data/gaokao_v4.db

EXPOSE 10000

CMD ["gunicorn", "backend.app_v3:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120"]
