"""What a Silicon did, kept so it can be read back.

    logs        one file per agent, forever, plus the inference trail
    journal     every command, run, message and file write, as it happened
    store       diagnosis traces and their rollups
    push        shipping those traces to Glass
    retention   the trace index
    activity    the per-contact activity trail
    cli         reading a run back

The `iwantto` CLI itself is not here — that is how a Silicon acts, and it
lives in ``iwantto/``. Only its journal is a diagnostic.
"""
