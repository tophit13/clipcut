#!/bin/sh
exec gunicorn server:app --bind "0.0.0.0:${PORT:-8080}" --workers 1 --threads 4 --timeout 300
