"""Optional LLM-provider plugins for calfcord agents.

Each subpackage adds a provider that AgentFactory can dispatch to based
on an agent's ``provider`` frontmatter field. Plugins are imported lazily
from ``agents.factory._default_model_client_factory`` so projects that
don't use them pay no import cost.
"""
