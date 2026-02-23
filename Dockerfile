FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY status_tracker/ status_tracker/
COPY templates/ templates/
COPY simple_tracker.py webhook_tracker.py ./

EXPOSE 8080

CMD ["python", "webhook_tracker.py", "--simulate-hub"]
