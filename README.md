# Xbox Wallpaper Changer

Turns the official wallpapers from [xbox.com/wallpapers](https://www.xbox.com/en-US/wallpapers) into a
Windows theme. Your desktop and lock screen rotate through them, and new wallpapers are downloaded
every week.

- **Exact resolution only.** Downloads only wallpapers offered in exactly the size you configure
  (default **1920x1080**). Wallpapers without that size are skipped, and every file is checked after
  download, so a mislabelled 1920x1000 image won't get in.
- **A real Windows theme.** "Xbox Wallpapers" is listed in *Settings > Personalization > Themes*, next
  to the built-in themes. It uses the Windows slideshow, so **right-click the desktop > Next desktop
  background** jumps to another random wallpaper. Pick another theme to turn it off, and pick it again
  to turn it back on.
- **Lock screen too.** It shows the same wallpaper as the desktop, or a different random one. It is only
  changed while the Xbox theme is showing.
- **Weekly updates.** It checks xbox.com every Sunday for new wallpapers.
- **No account needed.** It reads the public wallpaper list and never logs in anywhere.

## Requirements

- Windows 10 or 11
- [Python 3.10+](https://www.python.org/downloads/) on `PATH`
- No admin rights. Everything is installed per user.

## Install

```powershell
git clone https://github.com/<you>/XBOXWallpaper.git
cd XBOXWallpaper
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

What the installer does:

1. Copies the app to `%LOCALAPPDATA%\XboxWallpaper` and creates a Python virtual environment there.
2. Adds the `xwp` command to your user `PATH`. Open a new terminal to use it.
3. **First sync:** downloads every wallpaper available in 1920x1080 to
   `Pictures\Xbox Wallpapers\1920x1080`. That's about 600 files and 800 MB, and takes 10-15 minutes.
4. Installs the **Xbox Wallpapers** theme, switches to it and sets the lock screen.
5. Registers two tasks in Task Scheduler, under `\XboxWallpaper\`:

   | Task | When | What it does |
   |---|---|---|
   | **Sync** | Every Sunday at 12:00, or the next time the PC is on if it missed that time | Downloads wallpapers added to xbox.com since the last run |
   | **Watch** | At logon. Keeps running in the background and uses no CPU while waiting | Updates the lock screen when the wallpaper changes, and applies `config.json` edits within a minute |

You can run the installer again at any time to update. Wallpapers that are already downloaded are
not downloaded again.

## What to expect

- Right after the install, the desktop shows a random Xbox wallpaper, and the lock screen shows the same
  one (with the default config).
- The wallpaper changes every 60 minutes by default. When it changes, the lock screen follows within a
  couple of seconds.
- *Settings > Personalization > Themes* lists **Xbox Wallpapers (N images)**. Its accent color is Xbox
  green, and your light/dark mode is kept.
- Every Sunday, new wallpapers appear in the folder and join the rotation automatically.
- If you choose another theme or background, the app leaves your desktop and lock screen alone until you
  pick **Xbox Wallpapers** again (or run `xwp apply`).

## Configuration

Edit `%LOCALAPPDATA%\XboxWallpaper\config.json`. Changes take effect within a minute, with no restart.

```json
{
  "wallpaper_dir": "C:\\Users\\you\\Pictures\\Xbox Wallpapers",
  "resolution": "1920x1080",
  "interval_minutes": 60,
  "shuffle": true,
  "wallpaper_style": "fill",
  "lock_screen": {
    "enabled": true,
    "same_as_desktop": true
  },
  "delay_seconds": 0.5
}
```

| Key | Default | Meaning |
|---|---|---|
| `resolution` | `1920x1080` | Only wallpapers in exactly this size are downloaded. Sizes xbox.com offers: `1920x1080`, `2560x1440`, `2058x1440`, `2048x2048`, `1080x1920`. Each resolution gets its own subfolder. After changing it, run `xwp sync` and then `xwp apply`. |
| `interval_minutes` | `60` | How often the wallpaper changes. |
| `shuffle` | `true` | Random order. `false` goes through the wallpapers in order. |
| `lock_screen.enabled` | `true` | Also change the lock screen. |
| `lock_screen.same_as_desktop` | `true` | `true`: the lock screen shows the same picture as the desktop. `false`: a different random one each time the desktop changes. |
| `wallpaper_style` | `fill` | `fill`, `fit`, `stretch`, `center`, `span`, `tile` |
| `wallpaper_dir` | `Pictures\Xbox Wallpapers` | Where wallpapers are stored. You can drop your own images in the resolution subfolder too. |
| `delay_seconds` | `0.5` | Pause between downloads, to be gentle with the site. |

## Commands

| Command | What it does |
|---|---|
| `xwp next` | Show the next wallpaper now (same as right-click > *Next desktop background*). |
| `xwp sync` | Check xbox.com for new wallpapers now. |
| `xwp sync --dry-run` | List what would be downloaded, without downloading anything. |
| `xwp apply` | Switch to the Xbox Wallpapers theme (also rewrites the theme file from the config). |
| `xwp lock` | Update the lock screen now. |
| `xwp status` | Show the number of wallpapers, whether the theme is active, the current wallpaper and the last sync. |
| `xwp watch` | The background helper. Normally started by the *Watch* task, so you don't need to run it yourself. |

Add `-v` to any command to print the log to the console as well. The log file is
`%LOCALAPPDATA%\XboxWallpaper\xwp.log`.

**Don't like a wallpaper?** Delete it from the folder. It won't be downloaded again.

## How it works

- The xbox.com wallpapers page loads its whole catalog from one JSON file
  (`/en-US/wallpapers/JS/wallpapers-Sheet.js`). Each entry links up to six sizes (console, widescreen,
  standard, tablet, mobile, preview).
- Most file names include the size, for example `..._1920x1080.jpg`. For older ones that don't, the app
  reads only the first 64 KB of the image to find its size, and remembers the result.
- The theme is a standard `.theme` file in `%LOCALAPPDATA%\Microsoft\Windows\Themes` with a
  `[Slideshow]` section. The app also controls the slideshow directly, through the Windows
  `IDesktopWallpaper` API.
- The lock screen is set with the Windows `LockScreen.SetImageFileAsync` API.

## Notes & troubleshooting

- **Lock screen and Windows spotlight:** the first time the app sets the lock screen, Windows switches it
  from *Windows spotlight* to *Picture*. If you later set `lock_screen.enabled` to `false`, turn spotlight
  back on in *Settings > Personalization > Lock screen*.
- **Installing from a terminal inside an app** (for example the Claude desktop app or other Store-packaged
  apps): Windows silently redirects those apps' writes to `%LOCALAPPDATA%` into a private folder that
  Task Scheduler can't see. The installer and uninstaller detect this and re-run themselves through a
  one-time scheduled task, so it just works. Edits to `config.json` should still be made from a normal
  terminal or Explorer.
- **Other wallpaper tools:** anything else that changes the wallpaper on a schedule (for example the Nintendo
  Wallpaper Changer's hourly *Rotate* task) replaces the slideshow. Disable it if you want the Xbox theme.
- **If the site changes layout:** `xwp sync` logs "Wallpaper list not found" and nothing else is affected.
  Your existing collection keeps rotating.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\XboxWallpaper\uninstall.ps1"
```

This removes the scheduled tasks, the theme and the app folder. Your downloaded wallpapers in
`Pictures\Xbox Wallpapers` are kept. Pick another theme in Settings if the Xbox slideshow is still
showing.

## Disclaimer

This is an unofficial personal project, not affiliated with or endorsed by Microsoft or Xbox. The
wallpapers are © their respective owners and are downloaded from xbox.com for personal use.
