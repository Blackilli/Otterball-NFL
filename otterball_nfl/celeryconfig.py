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

beat_schedule = {
    "update-games-every-five-minutes": {
        "task": "otterball_nfl.tasks.update_games",
        "schedule": crontab(minute="*/5"),
        "args": (2025,),
    },
    "update-game_scores-every-two-minutes": {
        "task": "otterball_nfl.tasks.update_scores",
        "schedule": crontab(minute="*/2"),
    },
    "create-new-polls-every-wednesday": {
        "task": "otterball_nfl.tasks.create_polls",
        "schedule": crontab(day_of_week="wednesday", hour="18", minute="0"),
    },
}
