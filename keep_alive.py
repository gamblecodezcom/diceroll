"""
Lightweight HTTP server so hosts (Replit, fps.ms, etc.) keep the process alive
and can health-check the deployment.
"""
import os
import threading

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    return "Dice Hunt bot is running.", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def keep_alive():
    """Start Flask in a daemon thread; safe to call once from main."""
    port = int(os.environ.get("PORT", "8080"))
    bind = os.environ.get("BIND", "0.0.0.0")

    def run():
        # threaded=True so a slow health check never blocks the bot process
        app.run(host=bind, port=port, threaded=True, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
