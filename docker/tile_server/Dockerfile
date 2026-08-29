FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# 2 workers × 4 threads = 8 concurrent requests; each request holds a DB
# connection only for the duration of the query, so this is well within the
# default Supabase/pgBouncer connection pool limits.
CMD ["gunicorn", \
     "--workers", "2", \
     "--threads", "4", \
     "--bind", "0.0.0.0:8080", \
     "--timeout", "30", \
     "--access-logfile", "-", \
     "app:app"]
