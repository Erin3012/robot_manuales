from __future__ import annotations

import argparse
import os

import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible", action="store_true", help="Open the browser in visible mode by default")
    parser.add_argument("--headless", action="store_true", help="Open the browser in headless mode by default")
    args = parser.parse_args()

    default_headless = True
    if args.visible:
        default_headless = False
    elif args.headless:
        default_headless = True

    os.environ["SITFA_WEB_HEADLESS"] = "1" if default_headless else "0"
    os.environ.setdefault("SITFA_WEB_BROWSER", "edge")
    uvicorn.run("web_app:app", host="127.0.0.1", port=8050, reload=False)

