FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    mininet \
    openvswitch-switch \
    tshark \
    iperf \
    curl \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

# Ensure ryu_controller is importable
ENV PYTHONPATH=/app

EXPOSE 6633 6634 5000 8000

CMD ["ryu-manager", "--observe-links", "ryu_controller/sdn_controller.py"]
