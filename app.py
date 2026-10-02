"""Website that displays the trading bot's live status.

Read-only — this page never places trades itself. It just reads state.json,
which bot.py (running separately) writes to after every cycle. Run bot.py
and this as two separate, always-running processes.

When deployed (not localhost), this requires a password via HTTP Basic Auth,
set with the DASHBOARD_PASSWORD environment variable — never commit that
password or put it in a file that gets uploaded anywhere.
"""

from functools import wraps
from flask import Flask, jsonify, send_from_directory, request, Response
import os

import config

app = Flask(__name__, static_folder="static", static_url_path="")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


def require_password(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return view(*args, **kwargs)  # no password set (e.g. running locally) — open access
        auth = request.authorization
        if not auth or auth.password != DASHBOARD_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Trading Bot"'})
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
@require_password
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
@app.route("/state.json")
@require_password
def status():
    if not os.path.exists(config.STATE_FILE):
        return jsonify({"error": "No data yet — make sure bot.py is running."}), 404
    with open(config.STATE_FILE, "r") as f:
        return f.read(), 200, {"Content-Type": "application/json"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    if host == "0.0.0.0" and not DASHBOARD_PASSWORD:
        print("WARNING: running on a public host with no DASHBOARD_PASSWORD set — anyone with the URL could see your account.")
    print(f"Dashboard running at http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    app.run(host=host, port=port, debug=False)
