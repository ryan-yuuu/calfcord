"""Build-time CLIs that produce slim per-agent or per-tool Docker images.

calfcord's runtime architecture is location-transparent — tools subscribe
to fixed Kafka topics, so a tool node's body can live on a separate host
from the agent that invokes it. The all-in-one image shipped at
``Dockerfile`` is a convenience that hides this property; the CLIs here
surface it.

Two entry points (wired in ``pyproject.toml``):

* ``calfcord-package-tools`` — produces an image hosting only the named
  tools. The image bakes ``CALFCORD_TOOLS_INCLUDE`` so the auto-discovery
  loader skips registration of anything outside the list.
* ``calfcord-package-agents`` — produces an image hosting only the named
  agents. The Dockerfile COPYies just the selected ``agents/<name>.md``
  files; the runner already loads "whatever's in the agents dir," so no
  runtime filter is required.

Neither command pushes to a registry. Operators run ``docker push`` to
whichever registry they choose after the build.

See ``docs/distributed-deployment.md`` for the operator-facing
walkthrough and ``docs/security.md`` for the broker-auth implications
of running calfcord across multiple hosts.
"""
