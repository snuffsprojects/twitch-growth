FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

RUN mkdir -p /data && useradd --create-home --uid 10001 twitch-growth && chown -R twitch-growth:twitch-growth /app /data
USER twitch-growth

EXPOSE 5120
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
