import subprocess


def call_carbon(carbon_id: str, message: str = "") -> str:
    """Shell out to silicon-ring CLI, return status string."""
    cmd = ["silicon-ring", "call", carbon_id]
    if message:
        cmd += ["--message", message]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.stdout.strip() or result.stderr.strip() or "Call initiated"
    except FileNotFoundError:
        return "Error: silicon-ring CLI not installed. Run: pip install -e /path/to/silicon-ring"
    except subprocess.TimeoutExpired:
        return "Error: silicon-ring CLI timed out"
    except Exception as e:
        return f"Error: {e}"
