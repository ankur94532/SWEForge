# Optional repository MCP configuration

Copy `servers.example.yaml` to `servers.yaml`, replace the demonstration
connection, and list only tools this repository's workflow may reference.

The installed server definitions and allowlists are frozen with the repository
configuration generation. External server behavior remains controlled by that
trusted service.

Local `stdio` servers may declare `secret_env` references. Remote HTTP servers
may declare fixed `headers` separately from `secret_headers`, whose values are
repository-secret references. SWEForge resolves current values when starting a
fresh MCP client; values are not stored in this bundle or shown to the model.
Secret-authenticated remote endpoints must use HTTPS and redirects are disabled
so credentials remain on the configured origin.
