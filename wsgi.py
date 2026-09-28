# -*- coding: utf-8 -*-
"""WSGI entry point.

Two reasons this file exists instead of pointing a WSGI server straight at
"FAI Server.py":

1. Gunicorn takes a ``module:attr`` target and *imports* it. Python module
   paths cannot contain spaces, and the server file is named "FAI Server.py",
   so ``gunicorn "FAI Server.py":app`` can never import it - the error you get
   is an ImportError, not a helpful "module not found".

2. Uvicorn is an ASGI server and cannot serve a Flask (WSGI) app at all. This
   project is Flask, so it needs a WSGI server (Gunicorn).

Importing the module here also deliberately does NOT run the
``if __name__ == "__main__"`` block, which is what keeps the Windows-only
startup (certutil trust install, binding 80/443) from firing on Linux.
"""
import importlib.util
import pathlib

_SERVER_FILE = pathlib.Path(__file__).resolve().parent / "FAI Server.py"

_spec = importlib.util.spec_from_file_location("fai_server", _SERVER_FILE)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise ImportError(f"cannot load server module from {_SERVER_FILE}")

fai_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fai_server)

#: The Flask application object gunicorn should serve.
app = fai_server.app

if __name__ == "__main__":  # pragma: no cover - local convenience only
    # Bound to 127.0.0.1 / 5000 so it can be poked at on a dev box without
    # needing Administrator to bind 80/443.
    fai_server.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
