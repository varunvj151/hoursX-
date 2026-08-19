"""HoursX command-line interface.

``hoursx`` is a first-class way to use the platform, not a wrapper around the
HTTP API: agent commands run the same runtime in-process against SQLite, so an
operator can drive an agent on a host with no server, no queue, and no database
to provision.

Command groups::

    serve / worker / db-init / create-user   server lifecycle
    agent run|list|create                    drive agents
    chat                                     interactive REPL
    run list|show|cancel                     inspect and control runs
    approvals list|approve|deny              the human gate
    knowledge add|search                     retrieval corpus
    system probe|processes|kernel-log        host and kernel inspection
    doctor                                   configuration diagnosis
    gui                                      desktop application
"""

from hoursx.cli.main import build_parser, main

__all__ = ["build_parser", "main"]
