import os
import sys

PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.dirname(PACKAGE_ROOT)
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from frappe_auto_deploy.web_api import run_web_app


if __name__ == "__main__":
    run_web_app(open_browser=True)
