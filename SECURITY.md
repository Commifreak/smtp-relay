# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them privately through
**Security → Report a vulnerability** on the GitHub repository page instead. You will get an answer
as soon as possible, and the fix will be released with a note in the release.

## Supported versions

Only the latest release receives security fixes. Deployments that use the major version tag
(`ghcr.io/commifreak/smtp-relay:1`) get them with `docker compose pull`.

## Deployment notes

- Do not expose the SMTP port to the internet; smtp-relay is meant for trusted local networks.
- Publish the web UI only through HTTPS (reverse proxy) if it is reachable from outside your LAN.
- Complete the setup wizard right after the first start; until then, anyone who can read the container
  log can log in with the generated initial password.
