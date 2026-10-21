# Browser Tools

Web page navigation, interaction, and screenshot capture via headless Chromium.

## Prerequisite

The `ductor-browser` container must be running (see compose.yaml).

Browser requests carry the `BROWSER_TOKEN` of the environment, and the browser container
refuses requests without the matching token. The token is set once in
`~/.ductor/.env.shared`, which Compose gives to both the bot and the browser service. Do not
copy it into other files.

## Usage

```bash
python3 tools/browser_tools/browser.py <command> [args]
```

## Commands

- `navigate [--insecure] <url>` — navigate to a page
  - Use `--insecure` only for known internal hosts with self-signed certificates; use normal navigation for public websites.
- `snapshot` — get page structure (accessibility tree with ref numbers)
- `act <action> <ref> [value]` — interact with an element
  - Actions: `click`, `type`, `hover`, `scroll`, `select`
- `screenshot [--full]` — capture screenshot, returns file path
- `evaluate <expression>` — run JavaScript expression (quote it as one argument)
- `health` — check browser container status
- `fill_secret.py --origin <ORIGIN> <css_selector> <SECRET_KEY>` (separate script) — fill a form field from the secrets store only; requires an HTTPS origin, a secure context, an exact `location.origin` match and certificate verification; the value is never printed

## Workflow

1. `navigate` to the target page
2. `snapshot` to see element structure (note ref numbers)
3. `act` to interact with elements using their ref number
4. `screenshot` to verify the result

Ref numbers from `snapshot` are used by `act` to target elements — more
reliable than CSS selectors when the page structure changes.
