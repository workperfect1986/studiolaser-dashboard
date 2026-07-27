web: gunicorn --chdir dashboard -b 0.0.0.0:${PORT:-8765} --workers 1 --threads 8 --timeout 180 --access-logfile - --error-logfile - server:app
