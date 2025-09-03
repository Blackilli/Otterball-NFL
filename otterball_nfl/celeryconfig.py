from otterball_nfl import settings
from celery.schedules import crontab

broker_url = settings.CELERY_BROKER_URL
result_backend = "db+" + settings.DB_CONNECTION_STRING

task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "Europe/Berlin"
enable_utc = True
database_create_tables_at_setup = True

from celery.schedules import crontab

beat_schedule = {
    "update-games-every-five-minutes": {
        "task": "otterball_nfl.tasks.update_games",
        "schedule": crontab(minute="*/5"),
        "args": (2025,),
    },
}
