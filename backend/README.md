# BACK END SERVICE DEFINITIONS

## Services
- `backend` - The Django web appliciation running our REST API and handling our data models
- `celery-worker` - The async task queue Celery, processing tasks on a schedule
- `celery-beat` - The service that allows us to manage cron schedules through the DB, and therefore the UI

All of this will be part of a single Django application that will include the necessary `celery` and `celery-beat` libraries.
