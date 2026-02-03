import os

def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Required environment variable '{name}' is not set or is empty")
    return value