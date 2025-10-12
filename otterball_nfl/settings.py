import logging
from os import environ

DISCORD_BOT_TOKEN = environ.get("DISCORD_BOT_TOKEN")

POSTGRES_USER = environ.get("POSTGRES_USER")
POSTGRES_PASSWORD = environ.get("POSTGRES_PASSWORD")
POSTGRES_DB = environ.get("POSTGRES_DB")
POSTGRES_HOSTNAME = environ.get("POSTGRES_HOSTNAME", "db")
POSTGRES_PORT = environ.get("POSTGRES_PORT", 5432)
DB_CONNECTION_STRING = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOSTNAME}:{POSTGRES_PORT}/{POSTGRES_DB}"
# DB_CONNECTION_STRING = "sqlite:///db.sqlite3"

RABBITMQ_HOSTNAME = environ.get("RABBITMQ_HOSTNAME", "rabbitmq")
RABBITMQ_USER = environ.get("RABBITMQ_DEFAULT_USER")
RABBITMQ_PASS = environ.get("RABBITMQ_DEFAULT_PASS")
RABBITMQ_VHOST = environ.get("RABBITMQ_DEFAULT_VHOST", "my_vhost")
RABBITMQ_PORT = environ.get("RABBITMQ_NODE_PORT", 5672)
CELERY_BROKER_URL = f"amqp://{RABBITMQ_USER}:{RABBITMQ_PASS}@{RABBITMQ_HOSTNAME}:{RABBITMQ_PORT}/{RABBITMQ_VHOST}"

match environ.get("LOG_LEVEL", "INFO"):
    case "DEBUG":
        LOG_LEVEL = logging.DEBUG
    case "INFO":
        LOG_LEVEL = logging.INFO
    case "WARNING":
        LOG_LEVEL = logging.WARNING
    case "ERROR":
        LOG_LEVEL = logging.ERROR
    case "CRITICAL":
        LOG_LEVEL = logging.CRITICAL
    case _:
        LOG_LEVEL = logging.INFO
