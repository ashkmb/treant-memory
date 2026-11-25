"""macOS Keychain token lookup. Tokens live only there (service ``treant``, account = workspace)."""

import subprocess

SERVICE = "treant"


def get_token(ws: str, run=subprocess.run) -> str | None:
    try:
        r = run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", ws, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def set_token(ws: str, run=subprocess.run) -> bool:
    """Add or update the token; ``-w`` last with no value makes ``security`` prompt on the tty."""
    try:
        r = run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ws, "-w"])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
