FROM python:3.12-slim          # base OS + Python, the slim variant to stay small
WORKDIR /app                   # everything happens in /app inside the container
COPY requirements.txt .        # copy deps list first…
RUN pip install --no-cache-dir -r requirements.txt   # …install, so this layer caches
COPY . .                       # copy the rest of the loader's code in
CMD ["python", "job.py"]       # what runs when the job executes