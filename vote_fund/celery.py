import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vote_fund.settings')

app = Celery('flexyvotes')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
