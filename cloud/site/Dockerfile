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

# Every step must succeed (a failed download stops the build instead of being cached).
# A current pip is needed: the one in the base image mis-reads a dependency on
# PyTorch's index. Dependencies come from PyPI; torch itself from the CPU index.
RUN set -e \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --retries 10 --timeout 120 --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple "torch>=2.4" "torchvision>=0.19" \
 && pip install --no-cache-dir --retries 10 --timeout 120 "ultralytics>=8.0" "numpy>=1.24,<2" \
        "flask>=3.0" "requests>=2.31" "scipy>=1.11" "scikit-learn>=1.3" "twilio>=9.0" "lap>=0.5.12" \
 && (pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python || true) \
 && pip install --no-cache-dir "opencv-python-headless>=4.8,<4.12" "numpy>=1.24,<2" \
 && python -c "import cv2, torch, ultralytics; assert hasattr(cv2, 'FaceDetectorYN'); print('OpenCV', cv2.__version__, 'torch', torch.__version__, 'ultralytics', ultralytics.__version__)"

WORKDIR /opt/vigil-code
COPY jetson-backend/*.py jetson-backend/vigil_bytetrack.yaml ./

# IDENTITY_V8 face matching models (OpenCV Zoo: YuNet MIT, SFace Apache-2.0).
# Turned off per camera with: vigilctl.py edit-camera homeXX --face off
RUN mkdir -p models && cd models \
 && Z=https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models \
 && curl -fsSL -o face_detection_yunet_2023mar.onnx $Z/face_detection_yunet/face_detection_yunet_2023mar.onnx \
 && curl -fsSL -o face_recognition_sface_2021dec.onnx $Z/face_recognition_sface/face_recognition_sface_2021dec.onnx \
 && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  face_detection_yunet_2023mar.onnx" | sha256sum -c - \
 && echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  face_recognition_sface_2021dec.onnx" | sha256sum -c -
COPY cloud/site/site_entry.sh cloud/site/site_jobs.py ./

# Bake the models into the image so a site starts without downloading anything.
ENV TORCH_HOME=/opt/torch_cache
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n-pose.pt')" \
 && python -c "import torch; torch.hub.load('facebookresearch/dinov2','dinov2_vits14',verbose=False)" \
 && ls -la /opt/vigil-code/yolov8n-pose.pt /opt/torch_cache

RUN chmod +x site_entry.sh
EXPOSE 5001
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/vigil-code/site_entry.sh"]
