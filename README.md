# 🦦🔄 ollemolle

Restore live Oh My Pi and Claude Code sessions into a single Ghostty window.

`ollemolle` discovers verified interactive sessions, saves their working directory and safe resume arguments, then recreates them as Ghostty tabs. Snapshots are stored locally with owner-only permissions; the repository contains no session data.

## Requirements

- macOS
- [Ghostty](https://ghostty.org/) installed at `/Applications/Ghostty.app`
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Oh My Pi and/or Claude Code

## Install

```sh
uv tool install git+https://github.com/johnnydevriese/ollemolle.git
```

## Usage

Save all discovered sessions:

```sh
ollemolle save
```

Inspect the saved snapshot:

```sh
ollemolle list
```

Check what a restore would launch:

```sh
ollemolle restore --dry-run
```

Restore the snapshot into a new Ghostty window:

```sh
ollemolle restore
```

Named snapshots are supported by every snapshot command:

```sh
ollemolle save --name before-reboot
ollemolle restore --name before-reboot
```

## Claude Code registration

Claude sessions are registered from a Claude Code hook. Configure the hook to pipe its JSON payload to:

```sh
ollemolle register-claude
```

The command accepts `SessionStart`, `UserPromptSubmit`, and `SessionEnd` hook events. It records only sessions whose Claude process is running beneath Ghostty, so unrelated Claude sessions are not restored.

## Storage and safety

- Snapshots default to `~/.local/state/ollemolle/` and are written with mode `0600` inside a mode `0700` directory.
- Set `OLLEMOLLE_STATE_DIR` to move snapshot storage.
- Restore preflight refuses stale sessions, duplicate session identities, unsafe resume arguments, missing working directories, and sessions that are already live.
- Restores are serialized with a local lock and each tab starts its saved command through `zsh -lic`.

## Development

```sh
uv sync
uv run pytest
uv run basedpyright
uv run ruff check .
```

## License

[MIT](LICENSE)
