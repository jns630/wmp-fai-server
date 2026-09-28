# Render also reads a Procfile for Python web services. This is the third
# place the start command can live (dashboard > render.yaml > Procfile), and
# it is the fallback that does not depend on the blueprint being picked up.
#
# NOTE: gunicorn has NO --host/--port flags. Those are uvicorn's. The correct
# flag is --bind HOST:PORT. Using --host here is what produced
#   "gunicorn: error: unrecognized arguments: --host 0.0.0.0 --port 10000"
web: gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile -
