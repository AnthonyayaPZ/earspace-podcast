"""Build an uploadable source release without local secrets, databases or environments."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
output = root / "artifacts" / "earspace-server.zip"
output.parent.mkdir(exist_ok=True)
files = [root / name for name in (".env.example", ".gitignore", ".gitattributes", ".dockerignore", "Dockerfile",
                                 "compose.yaml", "pyproject.toml", "requirements.lock", "README.md")]
for directory in ("app", "scripts", "deploy", "tests", "docs", ".github"):
    path = root / directory
    if path.is_dir():
        files.extend(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc")
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for file in sorted(set(files)):
        archive.write(file, file.relative_to(root).as_posix())
print(f"Release: {output}")
