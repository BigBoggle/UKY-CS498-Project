import db
import sys
from pathlib import Path
from flask import Flask, render_template

ROOT = Path(__file__).resolve().parent.parent

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)


@app.route("/")
def index():
    return render_template("index.html")


# Main Application
if __name__ == "__main__":
    # TODO: CLI Arguments for DB input
    app.run(host="0.0.0.0")  # Don't use like this in prod
