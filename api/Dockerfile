# base OS + Python, the slim variant to stay small
FROM python:3.12-slim
# everything happens in /app inside the container
WORKDIR /app
# copy deps list first, install, so this layer caches across redeploys
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# copy the rest of the API's code in
COPY . .
# Cloud Run sends traffic to $PORT (default 8080); bind the web server to it
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
