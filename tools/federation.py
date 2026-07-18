"""
Federation Layer - pluggable routing for cross-machine delegate_task.

This module is imported by delegate_tool.py when a task has a target
field set. It tries to delegate to the federation plugin first, and
falls back to a clear error if the plugin is not installed.

To install the federation plugin:
  1. Copy to ~/.hermes/plugins/federation/
  2. Run: hermes plugins enable federation
  3. Add machine config to config.yaml
"""

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to import from the federation plugin
_plugin_route_to_machine = None
_plugin_path = Path.home() / ".hermes" / "plugins" / "federation"

if _plugin_path.exists() and str(_plugin_path) not in sys.path:
    sys.path.insert(0, str(_plugin_path.parent))

try:
    from federation import route_to_machine as _plugin_route_to_machine
except ImportError:
    logger.debug("Federation plugin not installed at %s", _plugin_path)
except Exception as e:
    logger.debug("Federation plugin import failed: %s", e)


def route_to_machine(target, tasks, parent_agent=None):
    """Route tasks to a federated machine.

    Delegates to the federation plugin if installed, otherwise raises
    a clear error telling the user how to set it up.
    """
    if _plugin_route_to_machine is not None:
        return _plugin_route_to_machine(target, tasks, parent_agent)

    raise NotImplementedError(
        "Federation routing to " + repr(target) + " is not configured. "
        "Install the federation plugin:\n"
        "  1. Copy to ~/.hermes/plugins/federation/\n"
        "  2. Run: hermes plugins enable federation\n"
        "  3. Add machine config to config.yaml:\n"
        "     federation:\n"
        "       machines:\n"
        "         " + target + ":\n"
        "           host: " + target + ".tailnet\n"
        "           user: chris\n"
        "Or omit the 'target' field for local delegation."
    )
