# Codex Offline Switcher

A terminal tool for managing saved Codex accounts. It copies the file-backed authentication from `CODEX_HOME` (default `~/.codex`) into named profiles. Switching preserves the existing Codex configuration, skills, history and sessions.

![Terminal menu and quota output with sample profiles](docs/terminal-preview.png)

## Run

Requires Python 3.11 or newer. Installations that store credentials in the OS keyring are outside the supported workflow.

On Windows, run:

```powershell
py -3 codex_accounts.py
```

On macOS and Linux, run:

```sh
python3 codex_accounts.py
```

The menu adds the active account to a named profile, imports a package, exports the active account, switches profiles, checks quotas and signs out locally. Save your work before switching or signing out. The tool closes recognized Codex and VS Code processes before replacing or removing the local credential. If processes remain open, close them manually and retry.

## Transfer a profile

**Use each Codex account on only one computer at a time with this tool.** Close Codex on the source computer before using the transferred profile on the destination. The tool copies the sign-in credentials, and concurrent use can cause token refresh conflicts.

Use **Export active account** on the source machine, copy the resulting folder privately to the destination, and use **Import package** there. Each export contains `auth.json`, `SHA256.txt` and `manifest.txt`. Treat the export folder like a password and keep it out of Git.

Profile management is local. **Check quota and reset credits** makes an authenticated read to ChatGPT and reports the five-hour and weekly limits, the reset countdown, and any available reset credits. It uses the existing access token.

The terminal and live quota check have been exercised on Windows. Account changes were tested with dummy credentials on Windows. Native macOS and Linux runs are unverified.
