"""bobi-deploy — the Fly deployment engine for Bobi agent teams.

Delivers the `bobi deploy` / `bobi deploy-init` / `bobi destroy` commands into
the public `bobi` CLI via the `bobi.commands` entry-point group. Installing
this package alongside `bobi` makes the commands appear; without it, the CLI
is the local product only.

Depends on `bobi` (never the reverse — the public framework must not import
this package).
"""
