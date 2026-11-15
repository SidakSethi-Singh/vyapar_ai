import os
import re
import sys

# Add root directory to sys.path so main.py can be imported seamlessly on Vercel
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from main import app as original_app  # noqa: E402


async def app(scope, receive, send):
    if scope["type"] == "http":
        headers = dict(scope.get("headers", []))
        path = scope.get("path", "/")

        # Inspect headers for original path sent by Vercel's proxy/rewrites
        for header_name in (b"x-forwarded-uri", b"x-matched-path", b"x-original-uri"):
            if header_name in headers:
                raw_val = headers[header_name].decode("utf-8", errors="ignore").split("?")[0]
                if raw_val and not raw_val.startswith("/api/index"):
                    path = raw_val
                    break

        # Normalize /api/index.py, /api/index, /api prefixes
        clean_path = re.sub(r"^/api(?:/index(?:\.py)?)?", "", path)
        if not clean_path:
            clean_path = "/"

        scope["path"] = clean_path
        if "raw_path" in scope:
            scope["raw_path"] = clean_path.encode("utf-8")

    await original_app(scope, receive, send)
