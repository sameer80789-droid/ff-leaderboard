# gunicorn reads this file automatically (it sits next to app.py), so even a plain
# `gunicorn app:app` start command gets these settings.
#
# ONE worker on purpose: the leaderboard updater, the login sessions and the caches live
# inside the process. Several workers would each log in separately (more load on Garena).
# Threads let many visitors be served while a login / leaderboard fetch is waiting.
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers = 1
threads = 8
timeout = 120          # default 30s would kill the worker (and the updater) during a slow login
graceful_timeout = 30
keepalive = 5
accesslog = "-"
