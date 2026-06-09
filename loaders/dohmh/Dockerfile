# base OS + Python, the slim variant to stay small
FROM python:3.12-slim
# everything happens in /app inside the container
WORKDIR /app
# copy deps list first, install, so this layer caches across redeploys
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# copy the rest of the loader's code in
COPY . .
# what runs when the job executes
CMD ["python", "job.py"]
