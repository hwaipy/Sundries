FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY ingest_server.py /app/

# 8123 = ingest (POST /ingest/<board>), 8124 = viewer/web GUI
EXPOSE 8123 8124
VOLUME ["/data"]

CMD ["python", "ingest_server.py", \
     "--host", "0.0.0.0", \
     "--port", "8123", \
     "--web-port", "8124", \
     "--data", "/data"]
