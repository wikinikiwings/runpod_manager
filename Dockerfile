FROM python:3.12-slim

# Install runpodctl
ADD https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-amd64 /usr/local/bin/runpodctl
RUN chmod +x /usr/local/bin/runpodctl

# Install Flask
RUN pip install --no-cache-dir flask

WORKDIR /app
COPY runpod_manager.py .

# Data volume for DB + settings (survives container restarts)
VOLUME /app/data

ENV DATA_DIR=/app/data

EXPOSE 5001

CMD ["python", "runpod_manager.py", "--host", "0.0.0.0", "--port", "5001"]
