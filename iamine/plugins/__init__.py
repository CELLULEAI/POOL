"""Plugin loader — opt-in via CELLULE_ENTERPRISE env var.

The public cellule.ai deployment never sets this variable. Enterprise
clients running under commercial license + NDA can enable their licensed
plugins via CELLULE_ENTERPRISE=1.

Plugins are loaded from iamine_enterprise.plugins and may register hooks
into the Pool lifecycle (see core/assist.py for hook point examples).

This module is intentionally empty in the public AGPLv3 repo. Actual
enterprise plugins live in the private POOL-PRIVATE repository.
"""

import os
import logging

log = logging.getLogger("iamine.plugins")


def load_enterprise_plugins(pool) -> int:
    """Load enterprise plugins if the environment opts in.

    Returns the number of plugins loaded. The public build has no
    enterprise plugins, so this is a no-op unless the private package
    is installed alongside.
    """
    if os.getenv("CELLULE_ENTERPRISE") != "1":
        return 0

    loaded = 0
    try:
        import importlib
        mod = importlib.import_module("iamine_enterprise.plugins")
        if hasattr(mod, "register_all"):
            loaded = mod.register_all(pool)
            log.info(f"enterprise: loaded {loaded} private plugins")
    except ImportError:
        log.warning("CELLULE_ENTERPRISE=1 set but iamine_enterprise not installed")
    except Exception as e:
        log.error(f"enterprise plugin load failed: {e}")

    return loaded
