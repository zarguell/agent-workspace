FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx jinja2 websockets

COPY . /app
WORKDIR /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--loop", "asyncio"]
