# omp-dash

A local web dashboard for omp coding-agent sessions. One window, all your sessions.

## What it shows

One card per session: name, model, status (working, idle, ended), current todos, a one-sentence summary, and pinned links. Click a card to focus that session's terminal. Buttons send commands to the session: steer text, plan review choices, approval mode.

## Install

1. Copy `omp-dash.ts` and `server.py` to `~/.omp/agent/extensions/`.
2. Start the server: `python3 ~/.omp/agent/extensions/server.py`. The extension starts it on its own when a session opens and no server runs.
3. Open `http://127.0.0.1:8734`.

## Configuration

Server (`server.py`):

| Variable | Default | Purpose |
|---|---|---|
| `OMP_DASH_PORT` | `8734` | Listen port |
| `OMP_DASH_HOST` | `127.0.0.1` | Bind address |
| `OMP_DASH_STATE` | `./state.json` | Session state file |
| `OMP_DASH_LINKS` | unset | Path to `links.json` |

Extension (`omp-dash.ts`):

| Variable | Default | Purpose |
|---|---|---|
| `OMP_DASH_URL` | `http://127.0.0.1:8734/report` | Dashboard endpoint |
| `OMP_DASH_SERVER` | `./server.py` | Server script path |
| `OMP_STE_VALE_DIR` | unset | Folder with a `vale` config. Adds a style check for session summaries |

## Link patterns

Copy `links.json.sample` to `links.json`. Regex matches in session names and task text render as link chips. The sample file shows the format.

## Security

The dashboard has no login. Anyone who can reach the port can steer your agent sessions. Keep `OMP_DASH_HOST` at `127.0.0.1`. Do not bind to `0.0.0.0` on a shared network.

## Fonts

The page uses Cinzel and Alegreya. Both fonts use the Open Font License 1.1 from SIL. See `fonts/OFL.txt`.

## License

MIT. See `LICENSE`.
