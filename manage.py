#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


# Свой порт у dev-сервера этого проекта: на машине одновременно живут
# несколько проектов, и стандартный 8000 почти всегда занят чужим. Голый
# `manage.py runserver` поднимается на 8710, `BACKEND_PORT=…` его переносит;
# фронт (`frontend/vite.config.js`) читает ту же переменную и проксирует
# `/api` туда же. Прод это не трогает — там gunicorn в контейнере.
DEFAULT_BACKEND_PORT = "8710"


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
        from django.core.management.commands import runserver
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    # `staticfiles` подменяет runserver своим, но порт по умолчанию берёт
    # у базового класса — поэтому правим его там.
    runserver.Command.default_port = os.environ.get("BACKEND_PORT") or DEFAULT_BACKEND_PORT
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
