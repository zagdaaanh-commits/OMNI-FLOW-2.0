"""Gunicorn settings for production (uvicorn workers).

Scheduling runs in the dedicated `worker` service (SCHEDULER_MODE=worker), so web workers stay
stateless and can be scaled freely.
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '7860')}"
worker_class = "uvicorn_worker.UvicornWorker"
# Modest default: LLM calls are I/O bound and each worker holds its own DB pool.
workers = int(os.getenv("WEB_CONCURRENCY", str(min(4, multiprocessing.cpu_count() * 2 + 1))))

timeout = 120  # LLM / image-upload calls can be slow
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
preload_app = False

# Only nginx (same Docker network) reaches this port, so its forwarded headers are trusted.
forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus "%(a)s"'
worker_tmp_dir = "/tmp"  # rootfs may be read-only
