FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 EVENTLET_NO_GREENDNS=yes
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app
COPY requirements.txt .
RUN pip install --no-cache-dir pip==24.0 setuptools==57.5.0 wheel==0.42.0 \
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt
COPY . .
EXPOSE 6633 5000
CMD ["python", "start_ryu.py"]
