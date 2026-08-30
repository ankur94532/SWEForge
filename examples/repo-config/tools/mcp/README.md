# Optional repository MCP configuration

Copy `servers.example.yaml` to `servers.yaml`, replace the demonstration
connection, and list only tools this repository's workflow may reference.

The installed server definitions and allowlists are frozen with the repository
configuration generation. External server behavior remains controlled by that
trusted service.

Local `stdio` servers may declare `secret_env` references. SWEForge resolves
those values from the current repository secret store when starting a fresh MCP
client; values are not stored in this bundle or shown to the model. Remote MCP
header/auth secret references are intentionally unsupported in this version.
