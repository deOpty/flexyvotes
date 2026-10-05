"""Gunicorn configuration (used by the Docker image)."""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get('GUNICORN_WORKERS', max(2, multiprocessing.cpu_count() * 2 + 1)))
threads = int(os.environ.get('GUNICORN_THREADS', '4'))
worker_class = 'gthread'
timeout = int(os.environ.get('GUNICORN_TIMEOUT', '60'))
graceful_timeout = 30
keepalive = 5
# Recycle workers periodically to bound memory growth.
max_requests = int(os.environ.get('GUNICORN_MAX_REQUESTS', '2000'))
max_requests_jitter = 200
accesslog = '-'
errorlog = '-'
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss rid=%({x-request-id}o)s'
forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1')
limit_request_line = 8190


def child_exit(server, worker):
    """Drop a dead worker's Prometheus samples (multiprocess mode)."""
    if os.environ.get('PROMETHEUS_MULTIPROC_DIR'):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
