"""Codex profile manager and portable credential package utility."""

import argparse
import base64
import csv
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def store_path():
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "CodexAccountSwitcher"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexAccountSwitcher"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "CodexAccountSwitcher"


def identity(data):
    try:
        tokens = json.loads(data)["tokens"]
        account = tokens["account_id"]
        access = tokens["access_token"]
        if not isinstance(account, str) or not account.strip() or not isinstance(access, str) or not access.strip():
            raise ValueError
        return hashlib.sha256(account.encode()).hexdigest().upper()
    except (KeyError, TypeError, ValueError):
        raise ValueError("No usable ChatGPT account credential in auth.json.") from None


def assert_file_storage(location):
    try:
        with (location / "config.toml").open("rb") as config:
            configured = tomllib.load(config).get("cli_auth_credentials_store", "file")
    except FileNotFoundError:
        configured = "file"
    except (ValueError, UnicodeError):
        raise ValueError("Cannot determine credential storage from config.toml.") from None
    if configured != "file":
        raise ValueError("The configured credential store is not 'file'. A file-backed active session could not be verified.")


def read_active():
    location = codex_home()
    assert_file_storage(location)
    try:
        data = (location / "auth.json").read_bytes()
    except FileNotFoundError:
        raise ValueError("No active auth.json. Keyring-only credentials cannot be exported.") from None
    identity(data)
    return location, data


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)


def write_files(directory, data):
    digest = hashlib.sha256(data).hexdigest()
    write_private(directory / "auth.json", data)
    write_private(directory / "SHA256.txt", f"{digest}  auth.json\n".encode("ascii"))


def detected_version():
    executable = shutil.which("codex")
    if sys.platform == "win32":
        installed = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
        desktop_binaries = list(installed.glob("*/codex.exe"))
        if desktop_binaries:
            executable = max(desktop_binaries, key=lambda path: path.stat().st_mtime)
    if executable:
        try:
            output = subprocess.check_output([str(executable), "--version"], stderr=subprocess.DEVNULL,
                                             text=True, timeout=5)
            match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b", output)
            if match:
                return match[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return "unavailable"


def publish(parent, name, write):
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=parent) as temporary:
        stage = Path(temporary) / "ready"
        stage.mkdir(mode=0o700)
        write(stage)
        destination = parent / name
        if destination.exists():
            raise ValueError("That name already exists.")
        stage.rename(destination)
    return destination


def export_active(destination=None):
    location, data = read_active()
    parent = (destination if destination is not None else store_path() / "exports").resolve()
    if parent.is_relative_to(location):
        raise ValueError("The export destination must be outside CODEX_HOME.")
    now = datetime.now(timezone.utc)
    name = f"codex-auth-export-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    manifest = (
        f"Codex version: {detected_version()}\n"
        f"Source OS: {platform.system()}\n"
        f"Active CODEX_HOME: {location}\n"
        "Auth storage mode: file\n"
        f"File size: {len(data)} bytes\n"
        f"Creation date: {now.isoformat(timespec='seconds')}\n"
    )

    def write(destination):
        write_files(destination, data)
        write_private(destination / "manifest.txt", manifest.encode("utf-8"))

    return publish(parent, name, write)


def read_package(directory):
    data = (directory / "auth.json").read_bytes()
    checksum = (directory / "SHA256.txt").read_text(encoding="utf-8-sig").strip()
    match = re.fullmatch(r"(?:SHA256\s*\(auth\.json\)\s*=\s*)?([0-9a-f]{64})(?:[ \t]+\*?auth\.json)?",
                         checksum, re.IGNORECASE)
    if not match or match[1].lower() != hashlib.sha256(data).hexdigest():
        raise ValueError("Package SHA-256 does not match auth.json.")
    identity(data)
    return data


def profile_ids():
    root = store_path() / "profiles"
    return {path.parent.name: path.read_text(encoding="ascii").strip().upper()
            for path in root.glob("*/identity.sha256")
            if not path.parent.name.startswith(".") and (path.parent / "snapshots").is_dir()}


def save_profile(name, data):
    name = name.strip().lower()
    reserved = {"logout", "quota", "con", "prn", "aux", "nul"}
    if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name) or name in reserved
            or re.fullmatch(r"(?:com|lpt)[0-9]", name)):
        raise ValueError("Use 1–40 ASCII letters, digits, underscores or hyphens, starting with a letter or digit.")
    fingerprint = identity(data)
    existing = profile_ids()
    if name in existing and existing[name] != fingerprint:
        raise ValueError("That profile name belongs to a different account.")
    duplicate = next((label for label, value in existing.items() if value == fingerprint and label != name), None)
    if duplicate:
        raise ValueError(f"This account is already saved as {duplicate}. Use that name to update it.")

    root = store_path() / "profiles"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z-" + uuid.uuid4().hex
    if name in existing:
        publish(root / name / "snapshots", stamp, lambda stage: write_files(stage, data))
    else:
        def write(stage):
            write_private(stage / "identity.sha256", fingerprint.encode("ascii"))
            snapshot = stage / "snapshots" / stamp
            snapshot.mkdir(parents=True, mode=0o700)
            write_files(snapshot, data)

        publish(root, name, write)
    return name



def read_saved(name):
    saved = profile_ids()
    if name not in saved:
        raise ValueError("Unknown profile.")
    folder = store_path() / "profiles" / name / "snapshots"
    latest = max((entry for entry in folder.iterdir()
                  if entry.is_dir() and re.fullmatch(r"\d{8}T\d{9}Z-[a-f0-9]{32}", entry.name)),
                 default=None)
    if latest is None:
        raise ValueError("No saved authentication.")
    data = read_package(latest)
    if identity(data) != saved[name]:
        raise ValueError("Saved authentication belongs to a different account.")
    return data


def matching_processes():
    if os.name == "nt":
        output = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace", timeout=10)
        rows = [(int(row[1]), Path(row[0]).stem) for row in csv.reader(output.splitlines())
                if len(row) > 1 and row[1].isdigit()]
    else:
        output = subprocess.check_output(["ps", "-ax", "-o", "pid=", "-o", "comm="],
                                         text=True, errors="replace", timeout=10)
        rows = []
        for line in output.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2 and parts[0].isdigit():
                pid, command = parts
                app = next((part[:-4] for part in Path(command).parts if part.endswith(".app")), None)
                rows.append((int(pid), app or Path(command).name))
    pattern = re.compile(r"^(?:ChatGPT|(?:OpenAI[- ]?)?Codex|Code|VSCode|Visual Studio Code)(?:$|[- .])",
                         re.IGNORECASE)
    return [(pid, name) for pid, name in rows if pattern.match(name)
            and name.lower() != "codex-windows-sandbox-service"]


def close_clients():
    if os.environ.get("TERM_PROGRAM") == "vscode":
        raise ValueError("Use a standalone terminal outside VS Code.")
    running = matching_processes()
    if not running:
        return
    if os.name == "nt":
        helper = Path(__file__).with_name("Close-CodexAndVSCode.ps1")
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
        runtime = shutil.which("pwsh") or (bundled if bundled.is_file() else shutil.which("powershell.exe"))
        if helper.is_file() and runtime:
            subprocess.run([str(runtime), "-NoProfile", "-File", str(helper)], check=True)
        else:
            # Portable installations can close clients manually before retrying.
            raise ValueError("Close Codex, ChatGPT Desktop and VS Code, then retry.")
    else:
        own_output = subprocess.check_output(["ps", "-x", "-o", "pid="], text=True, timeout=10)
        own_pids = {int(pid) for pid in own_output.split() if pid.isdigit()}
        for pid, _ in running:
            if pid in own_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for _ in range(20):
            if not matching_processes():
                break
            time.sleep(0.5)
    assert_clients_closed()


def assert_clients_closed():
    running = matching_processes()
    if running:
        names = ", ".join(sorted({name for _, name in running}))
        raise ValueError(f"Close remaining clients and retry: {names}.")


def change_account(name):
    target = read_saved(name) if name is not None else None
    location = codex_home()
    path = location / "auth.json"
    assert_file_storage(location)
    if path.is_symlink():
        raise ValueError("The active auth.json is a filesystem link.")
    if name is None and not path.exists():
        print("Already signed out.")
        return
    if input("Save your work. Codex clients and VS Code may close. Continue [y/N]? ").strip().lower() != "y":
        return
    close_clients()
    assert_file_storage(location)
    if path.is_symlink():
        raise ValueError("The active auth.json is a filesystem link.")
    before = path.read_bytes() if path.exists() else None
    if before is not None:
        fingerprint = identity(before)
        backup = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        saved = publish(store_path() / "backups", backup, lambda stage: write_files(stage, before))
        print(f"Backup: {saved}")
        current = next((label for label, value in profile_ids().items() if value == fingerprint), None)
        if current:
            save_profile(current, before)
            if name == current:
                print(f"{current} is already active.")
                return
    # Use a private staging directory on the destination volume for atomic replacement.
    with tempfile.TemporaryDirectory(prefix=".account-switch-", dir=location) as temporary:
        staged = Path(temporary) / "auth.json"
        if target is not None:
            write_private(staged, target)
        assert_clients_closed()
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != before:
            raise ValueError("Active authentication changed during the operation. Retry.")
        if target is None:
            path.unlink(missing_ok=True)
        else:
            staged.replace(path)
    print(f"Active account: {name}. Reopen Codex." if name else "Signed out locally. Saved profiles are available.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_usage(endpoint, tokens):
    url = "https://chatgpt.com/backend-api/wham/" + endpoint
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tokens["access_token"],
        "ChatGPT-Account-Id": tokens["account_id"],
        "Accept": "application/json",
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        return json.load(response)


@lru_cache(maxsize=1)
def windows_terminal_colors():
    """Enable ANSI rendering in the classic Windows console as well."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = kernel.GetStdHandle(wintypes.DWORD(-11))
    mode = wintypes.DWORD()
    if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    return bool(os.environ.get("WT_SESSION"))


def style(text, code):
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = text.translate(str.maketrans({"█": "#", "░": ".", "─": "-", "›": ">"}))
    if (sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
            and (os.name != "nt" or windows_terminal_colors())):
        return f"\033[{code}m{text}\033[0m"
    return text


def show_window(label, window):
    if not window or not isinstance(window.get("used_percent"), (float, int)):
        print(f"  {label:7}  Unavailable")
        return
    remaining = min(100, max(0, 100 - window["used_percent"]))
    filled = round(remaining / 5)
    if 0 < remaining < 100:
        filled = min(19, max(1, filled))
    color = 91 if remaining <= 10 else 93 if remaining <= 30 else 92
    bar = style("█" * filled, color) + style("░" * (20 - filled), 90)
    reset_text = "time unavailable"
    if isinstance(window.get("reset_at"), int) and 0 < window["reset_at"] < 253402300799:
        reset = datetime.fromtimestamp(window["reset_at"], timezone.utc)
        minutes = max(0, -int((datetime.now(timezone.utc) - reset).total_seconds() // 60))
        days, remainder = divmod(minutes, 1440)
        hours, mins = divmod(remainder, 60)
        countdown = f"{days}d {hours}h {mins}m" if label == "Weekly" else f"{days * 24 + hours}h {mins}m"
        reset_text = f"{reset.astimezone():%Y-%m-%d %H:%M %z} ({countdown + ' left' if minutes else 'due now'})"
    print(f"  {label:7}  {bar}  {remaining:6.3g}% left")
    print(f"           {style('Resets ' + reset_text, 90)}")


def show_quotas():
    print("\n" + style("Account quota", "1;96"))
    try:
        _, active = read_active()
    except (ValueError, OSError):
        active = None
    for name, fingerprint in sorted(profile_ids().items()):
        print("\n" + style(name, "1;97"))
        try:
            data = active if active and identity(active) == fingerprint else read_saved(name)
            tokens = json.loads(data)["tokens"]
            usage = fetch_usage("usage", tokens)
            if usage.get("account_id") not in (None, tokens["account_id"]):
                print("  Service returned a different account.")
                continue
            limits = usage.get("rate_limit") or {}
            windows = {row.get("limit_window_seconds"): row for row in
                       (limits.get("primary_window"), limits.get("secondary_window")) if isinstance(row, dict)}
            show_window("5-hour", windows.get(18000))
            show_window("Weekly", windows.get(604800))
            count = (usage.get("rate_limit_reset_credits") or {}).get("available_count")
            if type(count) is not int or count < 0:
                try:
                    count = fetch_usage("rate-limit-reset-credits", tokens).get("available_count")
                except (OSError, ValueError, AttributeError):
                    count = None
            availability = f"{count} available" if type(count) is int and count >= 0 else "Availability unknown"
            print(f"  Reset credits  {style(availability, 96 if type(count) is int and count > 0 else 90)}")
        except urllib.error.HTTPError as error:
            print("  Sign in again to check quota." if error.code == 401 else f"  Quota unavailable (HTTP {error.code}).")
        except (OSError, ValueError, AttributeError):
            print("  Could not read account quota.")


def ask_profile(data):
    existing = next((name for name, value in profile_ids().items() if value == identity(data)), None)
    prompt = f"Profile name [{existing}]: " if existing else "Profile name (empty to cancel): "
    name = input(prompt).strip() or existing
    if name:
        print(f"Saved profile: {save_profile(name, data)}")


def folder_picker_commands(title):
    if sys.platform == "win32":
        # Keep the title separate from the script and run the native picker in an STA.
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
        runtimes = (shutil.which("pwsh.exe") or (str(bundled) if bundled.is_file() else None),
                    shutil.which("powershell.exe"))
        script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
try {
    $dialog.Description = [Console]::In.ReadLine()
    if ($dialog.PSObject.Properties['UseDescriptionForTitle']) {
        $dialog.UseDescriptionForTitle = $true
    }
    $dialog.SelectedPath = [Environment]::GetFolderPath('UserProfile')
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.SelectedPath)
    }
} finally {
    $dialog.Dispose()
}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        for powershell in filter(None, runtimes):
            yield [powershell, "-NoProfile", "-STA", "-EncodedCommand", encoded], (title + "\n").encode(), False
    elif sys.platform == "darwin":
        script = """
on run argv
    try
        return POSIX path of (choose folder with prompt (item 1 of argv))
    on error number -128
        return ""
    end try
end run
"""
        yield ["/usr/bin/osascript", "-e", script, title], b"", False
    elif sys.platform.startswith("linux") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        tools = ("kdialog", "zenity") if "kde" in desktop else ("zenity", "kdialog")
        for tool in tools:
            path = shutil.which(tool)
            if path is not None:
                args = ([path, "--title", title, "--getexistingdirectory", str(Path.home())] if tool == "kdialog"
                        else [path, "--file-selection", "--directory", f"--title={title}",
                              f"--filename={Path.home()}/"])
                yield args, b"", True


def choose_folder(title):
    for command, input_data, cancel_is_one in folder_picker_commands(title):
        try:
            result = subprocess.run(command, input=input_data, capture_output=True)
        except OSError:
            continue
        if result.returncode == 0:
            folder = os.fsdecode(result.stdout) if sys.platform.startswith("linux") else result.stdout.decode("utf-8")
            folder = folder.removesuffix("\r\n") if folder.endswith("\r\n") else folder.removesuffix("\n")
            if not folder:
                return None
            path = Path(folder)
            if path.is_dir():
                return path
            raise ValueError("The selected location is not an accessible folder.")
        if cancel_is_one and result.returncode == 1 and not re.search(
            rb"(cannot open display|failed to open display|could not connect to display|could not load the Qt platform plugin)",
            result.stderr, re.IGNORECASE
        ):
            return None
    folder = input(f"{title} (paste folder path, or Enter to cancel): ").strip()
    if not folder:
        return None
    if len(folder) >= 2 and folder[0] in "'\"" and folder[-1] == folder[0]:
        folder = folder[1:-1]
    path = Path(folder).expanduser()
    if not path.is_dir():
        raise ValueError("Choose an existing folder.")
    return path


def terminal_menu():
    while True:
        try:
            _, current = read_active()
            active = next((name for name, value in profile_ids().items() if value == identity(current)), "Unsaved account")
        except (ValueError, OSError) as error:
            active = str(error)
        print("\n" + style("Codex Offline Switcher", "1;96"))
        print(style("─" * 48, 90))
        print(f"  Active account   {style(active, '1;97')}")
        print("  Saved profiles   " + (", ".join(sorted(profile_ids())) or "(none)"))
        print("\n" + style("Switch and save profiles", "1;97"))
        print(f"  {style('1', 96)}  Switch to a saved profile")
        print(f"  {style('2', 96)}  Save this computer's active account")
        print(f"     {style('Copy the current Codex login into a named profile.', 90)}")
        print("\n" + style("Transfer between computers", "1;97"))
        print(f"  {style('3', 96)}  Import a profile from an export folder")
        print(f"     {style('Choose the folder containing auth.json and SHA256.txt.', 90)}")
        print(f"  {style('4', 96)}  Export this computer's active account")
        print("\n" + style("Status and sign-out", "1;97"))
        print(f"  {style('5', 96)}  Check quota and reset credits")
        print(f"  {style('6', 96)}  Sign out on this computer")
        print(f"  {style('0', 96)}  Exit menu")
        choice = input(style("\nSelect an option › ", 0)).strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                name = input("Profile name (empty to cancel): ").strip().lower()
                if name:
                    change_account(name)
            elif choice == "2":
                ask_profile(read_active()[1])
            elif choice == "3":
                folder = choose_folder("Select the package folder to import")
                if folder is not None:
                    ask_profile(read_package(folder))
            elif choice == "4":
                folder = choose_folder("Select where to create the export folder")
                if folder is not None:
                    print(f"Export created: {export_active(folder)}")
            elif choice == "5":
                show_quotas()
            elif choice == "6":
                change_account(None)
            else:
                print("Choose a displayed option.")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"Operation stopped: {error}")


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--export-active", action="store_true")
    actions.add_argument("--add-active", metavar="NAME")
    actions.add_argument("--import-package", nargs=2, metavar=("FOLDER", "NAME"))
    arguments = parser.parse_args()
    try:
        if arguments.export_active:
            print(export_active())
        elif arguments.add_active:
            print(save_profile(arguments.add_active, read_active()[1]))
        elif arguments.import_package:
            folder, name = arguments.import_package
            print(save_profile(name, read_package(Path(folder))))
        else:
            terminal_menu()
    except (EOFError, KeyboardInterrupt):
        print()
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
