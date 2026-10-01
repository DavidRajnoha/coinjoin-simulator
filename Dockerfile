FROM python:3.13

RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install psutil separately to avoid invalidating main requirements cache
RUN pip install --no-cache-dir psutil

COPY . .
RUN mkdir /app/logs && chown -R 1000:1000 /app/logs

ENV PYTHONPATH=/app
ENV IN_CLUSTER=true

CMD ["python", "-u", "manager.py", "--help"]
