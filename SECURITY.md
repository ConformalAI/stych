# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report them privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** (private vulnerability reporting). Include what you found, how to reproduce it, and the version or commit you tested.

We will acknowledge your report, keep you updated while we investigate, and credit you in the fix if you wish.

## Scope

This covers the code in this repository. For a self-hosted deployment, you are responsible for its configuration. At minimum:

- Set a strong, unique `SECRET_KEY` (it also encrypts saved data-source credentials).
- Run with `ENV=production` and `DEV_MODE=false`.
- Serve the app over HTTPS and keep the SuperTokens core and databases off the public network.
