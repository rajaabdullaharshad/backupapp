import os
import sys

# Ensure local package path is available when executing as script or bundled with PyInstaller.
PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.dirname(PACKAGE_ROOT)
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

try:
    from frappe_auto_deploy.ui_new import run_app
except ModuleNotFoundError:
    try:
        from ui_new import run_app
    except ModuleNotFoundError as fallback_exc:
        raise ModuleNotFoundError(
            "Failed to import run_app from frappe_auto_deploy.ui_new or ui_new. "
            "Run from project root with `python -m frappe_auto_deploy.main` or `python src/frappe_auto_deploy/main.py`."
        ) from fallback_exc


if __name__ == "__main__":
    sys.exit(run_app())
