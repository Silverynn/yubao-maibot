"""Only version local frontend assets. Never touch model files or user settings."""
from hashlib import sha256
from pathlib import Path
import re


def versioned_html(source: str, root: Path) -> str:
    root = root.resolve()

    def replace(match):
        attr, quote, url = match.groups()
        # Skip external URLs, paths outside frontend and arbitrary inline attributes.
        if not url.startswith(("./", "/")) or url.startswith("//"):
            return match.group(0)
        local = url.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        target = (root / local).resolve()
        if not target.is_relative_to(root) or target.suffix not in {".js", ".mjs", ".css"}:
            return match.group(0)
        if not target.is_file():
            return match.group(0)
        version = sha256(target.read_bytes()).hexdigest()[:16]
        return f'{attr}={quote}{url.split("?", 1)[0]}?heart_v={version}{quote}'

    return re.sub(r'(src|href)=(["\'])([^"\']+)\2', replace, source)
