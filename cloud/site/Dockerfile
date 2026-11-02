# One VIGIL "site" = one camera's full pipeline: the same Server.py and
# activity_monitor.py that run on the Jetson, unchanged, in a container.
# Build from the repository root:  docker build -f cloud/site/Dockerfile -t vigil-site .
FROM python:3.10-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    YOLO_CONFIG_DIR=/tmp/ultralytics

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 ffmpeg curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
 && pip install "ultralytics>=8.0" "opencv-python-headless>=4.6" "numpy>=1.24,<2" \
        "flask>=3.0" "requests>=2.31" "scipy>=1.11" "scikit-learn>=1.3" "twilio>=9.0" "lap>=0.5.12" \
 && pip uninstall -y opencv-python || true

WORKDIR /opt/vigil-code
COPY jetson-backend/*.py jetson-backend/vigil_bytetrack.yaml ./
COPY cloud/site/site_entry.sh cloud/site/site_jobs.py ./

# Bake the models into the image so a site starts without downloading anything.
ENV TORCH_HOME=/opt/torch_cache
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n-pose.pt')" \
 && python -c "import torch; torch.hub.load('facebookresearch/dinov2','dinov2_vits14',verbose=False)" \
 && ls -la /opt/vigil-code/yolov8n-pose.pt /opt/torch_cache

RUN chmod +x site_entry.sh
EXPOSE 5001
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/vigil-code/site_entry.sh"]
