# GitHub Tools

Helpers for working with GitHub from the workspace. Keys are read with `tools/_secrets.py`,
so a token or seed lives in the environment or the secrets store and never on a command line.

## totp.py

```bash
python3 tools/github_tools/totp.py <SECRET_KEY>
```

Prints the current six-digit one-time code (SHA-1, 30 s) for the base32 seed stored under
`SECRET_KEY`. The seed is never printed.

## Related tools in `tools/`

- `git_push.py <repo> [branch]` pushes over HTTPS with `GH_TOKEN` or `GITHUB_TOKEN`, only when
  the push URL of `origin` is `https://github.com/...` without credentials in it.
  The optional branch argument must be a local branch name, not a refspec.
  Proxy environment variables and user CA settings are ignored, so pushes cannot use
  an egress proxy or TLS interception with a corporate CA.
- `git_credential_from_ductor_env.sh` is a git credential helper that answers for
  `https://github.com` only. Register it with
  `git config --global credential.helper '!/path/to/git_credential_from_ductor_env.sh'`.
