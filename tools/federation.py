"""
Federation Layer - pluggable routing for cross-machine delegate_task.

This module is imported by delegate_tool.py when a task has a target
field set. The default stub raises a clear error; install the federation
skill to enable actual cross-machine routing.
"""


def route_to_machine(target, tasks, parent_agent=None):
    raise NotImplementedError(
        "Federation routing to " + repr(target) + " is not configured. "
        "Install the federation skill or provide a custom "
        "tools/federation.py with a route_to_machine() function."
    )
