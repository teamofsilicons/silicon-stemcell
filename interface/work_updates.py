"""The work package, under the name its callers and tests already use.

The implementation lives in ``interface/work/``. This stays because
``interface.work_updates.X`` is a patch target in string form across the test
suite, and because ten call sites import it lazily inside functions.

ponytail: delete once those move to interface.work.<module>.
"""
from interface.work import *  # noqa: F401,F403
from interface.work import WORK_UPDATES_FILE  # noqa: F401
