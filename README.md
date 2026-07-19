# CompanionAgent

CompanionAgent is a lightweight local sync agent for a Resolume Arena PC.
It polls a server/web-app media manifest, downloads new or changed images,
videos, and audio files, and stores them in local folders that Resolume can
read directly.

This avoids relying on SMB/Samba/network shared folders when Resolume only
detects local paths.

## What it does

- Runs continuously on the Resolume machine.
- Starts automatically at Windows logon or boot through Task Scheduler.
- Pulls media from your server/web app over HTTP/HTTPS.
- Saves files under a configured local folder, preserving subfolders.
- Skips files that are already downloaded and unchanged.
- Optionally removes local files that disappeared from the server manifest.
- Uses only the Python standard library; no pip dependencies are required.

## Files

- `resolume_media_agent.py` - the sync agent.
- `config.example.json` - copy this to `config.json` and edit it.
- `install_windows_task.ps1` - registers the agent to auto-run.
- `uninstall_windows_task.ps1` - removes the scheduled task.
- `sample_manifest.json` - example response your web app can expose.

## Server manifest format

Expose an endpoint from your web app that returns JSON like this:

```json
{
  "files": [
    {
      "path": "images/logo.png",
      "url": "https://server.example.com/media/images/logo.png",
      "sha256": "optional-file-sha256",
      "size": 123456
    },
    {
      "path": "videos/intro.mp4",
      "url": "/media/videos/intro.mp4"
    }
  ]
}
```

`url` may be absolute or relative to the manifest URL. `path` is the local
relative path under `media_root`.

## Setup on the Resolume PC

1. Install Python 3.10 or newer from <https://www.python.org/downloads/windows/>.
2. Copy this folder onto the Resolume PC, for example:

   ```text
   C:\CompanionAgent
   ```

3. Copy the example config:

   ```powershell
   Copy-Item C:\CompanionAgent\config.example.json C:\CompanionAgent\config.json
   ```

4. Edit `config.json`:

   - Set `manifest_url` to your server/web-app endpoint.
   - Set `media_root` to a local folder that Resolume will read, for example
     `C:\ResolumeMedia`.

5. Test one sync:

   ```powershell
   py -3 C:\CompanionAgent\resolume_media_agent.py --config C:\CompanionAgent\config.json --once
   ```

6. Install auto-run at user logon:

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\CompanionAgent\install_windows_task.ps1 -ConfigPath C:\CompanionAgent\config.json
   ```

   To run at machine startup as `SYSTEM`, open PowerShell as Administrator and
   add `-AtStartup -RunAsSystem`.

7. Open Resolume Arena and point your media browser/decks to the local
   `media_root` folder.

## Logs

By default logs are written beside the config file under:

```text
logs\resolume_media_agent.log
```

You can change this in `config.json`.

## Uninstall auto-run

```powershell
powershell -ExecutionPolicy Bypass -File C:\CompanionAgent\uninstall_windows_task.ps1
```

## Notes

- The agent downloads to temporary `.part` files and only replaces the final
  media file after the download is complete.
- Local path traversal and unsafe Windows filename characters are sanitized.
- If your server requires authentication, set `auth_token` in `config.json`.
  The token is sent as `Authorization: Bearer <token>`.
