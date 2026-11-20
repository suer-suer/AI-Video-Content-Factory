"""Save a KEY=VALUE pair into the workspace .env (secrets stay out of git)."""

from pathlib import Path


def save_key(env_path: str | Path, key: str, value: str) -> None:
    # strip newlines from the value: a multiline value would break the
    # KEY=VALUE line format and append junk lines to .env (review fix)
    value = str(value).replace("\r", " ").replace("\n", " ").strip()
    env_path = Path(env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
