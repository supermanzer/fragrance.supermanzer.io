# Importing celery_app here ensures the Celery application is created and registered
# with Django at package import time, before any task modules are loaded. Without this,
# tasks discovered during startup would have no app to register against.
from .celery import app as celery_app

# Declares celery_app as the intentional public export of this package.
__all__ = ("celery_app",)
