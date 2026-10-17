#!/usr/bin/env bash
set -e

# Wait for database connection before running migrations
echo "Verifying database readiness..."
python << 'EOF'
import os
import sys
import time
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'botgi_crm.settings')

try:
    django.setup()
    from django.db import connections
    max_retries = 30
    while max_retries > 0:
        try:
            conn = connections['default']
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1;")
            print("Database is ready!")
            sys.exit(0)
        except Exception as e:
            max_retries -= 1
            print(f"Waiting for database to become available... ({max_retries} attempts left): {e}")
            time.sleep(2)
    print("Could not connect to database in time.")
    sys.exit(1)
except Exception as setup_err:
    print(f"Warning during DB readiness check: {setup_err}")
EOF

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
