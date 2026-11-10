FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install "flask>=3.0" "requests>=2.31" "gunicorn>=22" "httpx[http2]>=0.27" "pyjwt[crypto]>=2.8"
WORKDIR /app
COPY gateway.py .
EXPOSE 8080
# gthread workers: live camera views hold a thread each while open.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "-k", "gthread", "--threads", "64", "--timeout", "0", "gateway:app"]
