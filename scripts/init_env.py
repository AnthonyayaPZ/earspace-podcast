"""Create a local secret without printing it or replacing an existing configuration."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    print(".env already exists; left unchanged.")
else:
    template = (root / ".env.example").read_text(encoding="utf-8")
    template = template.replace("replace-with-a-random-secret-at-least-24-characters", secrets.token_urlsafe(32))
    target.write_text(template, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print("Created .env with a random API key. Add your YOUTUBE_API_KEY before discovery.")
