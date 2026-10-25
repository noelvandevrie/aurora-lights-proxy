FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Unbuffered, so log lines show up in `docker compose logs` right away.
# Exec form: python is PID 1 and gets SIGTERM, so it publishes the bridge's offline status on `docker compose stop`.
CMD ["python", "-u", "main.py"]
