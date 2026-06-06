FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "run_multi.py"]
