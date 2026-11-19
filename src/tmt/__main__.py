"""``python -m tmt`` behaves exactly like the ``tmt`` command (the web UI relies on this)."""

from tmt.cli import app

if __name__ == "__main__":
    app()
