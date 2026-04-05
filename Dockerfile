# Dockerfile — for self-hosting or any container platform
# (Railway, Fly.io, AWS ECS, Google Cloud Run, etc.)
#
# Build:  docker build -t neighborhoodintel .
# Run:    docker run -p 5000:5000 -e FBI_API_KEY=your_key neighborhoodintel

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY neighborhood_crime_report.py .
COPY dashboard_v2.html .

# Create cache directory
RUN mkdir -p report_cache

# Expose port
EXPOSE 5000

# Run with gunicorn
CMD gunicorn app:app --workers 2 --timeout 60 --bind 0.0.0.0:${PORT:-5000}
