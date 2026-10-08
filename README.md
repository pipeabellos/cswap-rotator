# cswap-rotator

Per-request account rotation for Claude Code, on top of [claude-swap](https://github.com/realiti4/claude-swap) (cswap).

cswap switches the login Claude Code uses. Between subscriptions that works well, because Claude Code re-reads its login about every 30 seconds. It cannot move a running session between a subscription and an API key, though: a session decides at startup whether it talks to Anthropic with a subscription login or an API key. Switching cswap to a Console API key logs running sessions out ("Not logged in"), and switching back leaves sessions that started on the key quietly spending API credits.

cswap-rotator is a small local proxy that removes that limit. Claude Code sessions point `ANTHROPIC_BASE_URL` at it, and it picks the credential for every request:

1. **The subscription cswap has active**, until Anthropic refuses it for quota (or until `CSWAP_ROTATOR_FULL_PCT`, if you set one). If it is full: the account the model is already on, then the one with the most room.
2. **The Console API key** from cswap's API-key slot, once every subscription is full.
3. **Full subscriptions**, only if the API key is rate limited or out of credits.

Sessions never restart, never change auth mode, and never see a limit error while some account still has room. When a subscription's window resets, the next request goes back to it.

## How it decides, request by request

| Upstream answer | What the proxy does |
| --- | --- |
| 429 with `anthropic-ratelimit-unified-status: rejected` (account out of quota) | Parks that account for that model until its window resets, retries the request on the next account |
| 429 while the account still has quota (the request itself was refused, e.g. a huge uncached prompt) | Retries elsewhere without moving anyone; after two subscriptions refuse it, the API key handles that one request |
| 401 / 403 on a subscription | Next account, and re-reads cswap's logins |
| API key: `credit balance is too low` | Falls back to the least-full subscription |
| Network error | A retryable 502 on the same account; Claude Code retries by itself |
| Any response | Reads the live `anthropic-ratelimit-unified-*-utilization` headers; once an account reaches `CSWAP_ROTATOR_FULL_PCT` it stays "full" until that window resets |

## Install

cswap-rotator runs inside cswap's own environment, so both use the same cswap code on the same account store:

```bash
uv tool install claude-swap --with cswap-rotator --with-executables-from cswap-rotator
cswap-rotator install-service   # launchd agent on macOS, systemd user unit on Linux
cswap-rotator enable            # route NEW Claude Code sessions through the proxy
```

`enable` writes three variables into the `env` block of `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) and turns off cswap's own API-key fallback:

- `ANTHROPIC_BASE_URL=http://127.0.0.1:8890`
- `ENABLE_TOOL_SEARCH=true` and `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1`: with a custom base URL Claude Code turns MCP tool search and fine-grained tool streaming off, assuming the proxy can't forward them. This one can. Without tool search every MCP tool definition is sent upfront on every request, which can be hundreds of thousands of tokens.
- `cswap config set autoswitch.includeApiKeyAccounts false`: if cswap switched to the API key itself, it would clear the subscription login and log every proxied session out. The proxy handles the API key now.

Sessions that are already running keep their previous setup until they restart (`claude --resume` keeps the conversation). `cswap-rotator disable` undoes all of the above for new sessions.

On Windows there is no service install yet: run `cswap-rotator serve` at login.

## Use

```bash
cswap-rotator status     # which account answers each model, usage and token age per account
cswap-rotator restart    # restart the login service (safe while sessions run)
```

`/status` inside a proxied session shows the session's own login (cswap's active account) and `Anthropic base URL: http://127.0.0.1:8890`. The account that actually answered is in `cswap-rotator status`, or in your statusline:

```bash
# in a statusline script (Claude Code passes the session JSON on stdin)
account=$(echo "$input" | cswap-rotator statusline --color)
```

It prints `⇄ <account> <usage>%` in green, `⇄ API key` in red while Console credits are being spent, and `<account> (direct)` for sessions that don't use the proxy.

## Things to know

- **Every account move costs each session one re-send of its context.** Anthropic keeps prompt caches and Claude Code's server-side conversation state per account. After a move, a session's next request gets a `No thread state was found` 404, and Claude Code replays the conversation on its own. That is why the proxy follows cswap, stays put until an account is full, never moves for network errors or one-off refusals, and keeps its choices across restarts.
- **Other clients keep their own key.** A request that brings an API key other than cswap's (an app SDK that inherited `ANTHROPIC_BASE_URL`, `claude -p` with `ANTHROPIC_API_KEY` set) passes through untouched.
- **cswap stays the owner of credentials.** The proxy reads logins through cswap's own code, never refreshes a token (refresh tokens are single use) and never takes cswap's locks.
- **Local only.** It listens on 127.0.0.1 and refuses requests with an `Origin` header (browsers) or a foreign `Host` (DNS rebinding), so a web page cannot spend your quota through it.
- **Restarts are safe.** A request cut by a restart is retried by Claude Code.
- `/ultrareview` is hidden in sessions that use a custom base URL.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `CSWAP_ROTATOR_PORT` | `8890` | listen port (127.0.0.1 only) |
| `CSWAP_ROTATOR_FULL_PCT` | `100` | usage % at which a subscription counts as full; the default uses each one until Anthropic refuses it, since the proxy retries that request on the next account |
| `CSWAP_ROTATOR_HOME_DIR` | `<cswap dir>/rotator` | log and state files |
| `CSWAP_ROTATOR_LOG` | `<home>/rotator.log` | JSON lines: request, switch, full, cooldown, passthrough |
| `CSWAP_ROTATOR_STATE` | `<home>/state.json` | sticky choices, cooldowns, full-account latches |
| `CSWAP_ROTATOR_UPSTREAM` | `https://api.anthropic.com` | |

## Development

```bash
uv sync
uv run pytest
```

The tests run a real proxy process against a fake Anthropic API and a fake cswap; they never read real accounts.

## License

MIT
