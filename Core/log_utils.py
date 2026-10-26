import os


def debug(msg):
    """Traza técnica: solo se muestra con ARCHIE_DEBUG=1 (Config/.env)."""
    if os.getenv("ARCHIE_DEBUG", "0") == "1":
        print(f"  [DEBUG] {msg}")


def log_error(msg):
    print(f"  [ERROR] {msg}")
