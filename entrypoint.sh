#!/usr/bin/env bash
set -e

# Wait for PostgreSQL if DATABASE_URL is set
if [ -n "$DATABASE_URL" ]; then
    echo "Waiting for database connection..."
    python << 'EOF'
import os
import sys
import time
import urllib.parse

db_url = os.environ.get('DATABASE_URL', '')
if db_url.startswith(('postgres://', 'postgresql://')):
    import psycopg2
    # Normalize postgres:// to postgresql:// for psycopg2
    db_url_parsed = urllib.parse.urlparse(db_url)
    max_retries = 30
    while max_retries > 0:
        try:
            conn = psycopg2.connect(
                dbname=db_url_parsed.path[1:],
                user=db_url_parsed.username,
                password=db_url_parsed.password,
                host=db_url_parsed.hostname,
                port=db_url_parsed.port or 5432,
                connect_timeout=3
            )
            conn.close()
            print("Database is ready!")
            sys.exit(0)
        except Exception as e:
            max_retries -= 1
            print(f"Waiting for database... ({max_retries} attempts left): {e}")
            time.sleep(2)
    print("Could not connect to database in time.")
    sys.exit(1)
EOF
fi

# Run database migrations if RUN_MIGRATIONS is true (default: true for web service)
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput
fi

# Collect static files if COLLECT_STATIC is true (default: true for web service)
if [ "${COLLECT_STATIC:-true}" = "true" ]; then
    echo "Collecting static assets..."
    python manage.py collectstatic --noinput
fi

echo "Starting application..."
exec "$@"
