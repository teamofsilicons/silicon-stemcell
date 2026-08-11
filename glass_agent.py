"""Entry point for the Interface live agent.

The external `silicon` CLI starts and stops this sidecar by filename, so the
name stays even though the code moved to ``interface/agent/``.
"""
from interface.agent.live import main

if __name__ == "__main__":
    main()
