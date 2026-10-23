"""Xbox Wallpaper Changer.

Commands:
  sync     Download new wallpapers from xbox.com/wallpapers in the configured
           resolution (run weekly). Wallpapers without that exact size are skipped.
  apply    Install the "Xbox Wallpapers" Windows theme and switch to it.
  next     Show the next wallpaper (same as right-click > Next desktop background).
  lock     Update the lock screen now, according to the config.
  watch    Background helper: keeps the lock screen in sync with the desktop and
           applies config changes (started at logon).
  status   Show collection / theme status.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import msvcrt
import os
import random
import re
import sys
import time
import urllib.request
from ctypes import byref, wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

APP_DIR = Path(os.environ.get("XWP_HOME", Path(os.environ["LOCALAPPDATA"]) / "XboxWallpaper"))
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = APP_DIR / "xwp.log"
THEME_NAME = "Xbox Wallpapers"
THEME_PATH = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Themes" / f"{THEME_NAME}.theme"

LIST_URL = "https://www.xbox.com/en-US/wallpapers/JS/wallpapers-Sheet.js"
ASSET_BASE = "https://assets.xboxservices.com/assets"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Variant keys in the site's list, in the order we look for the wanted size.
VARIANTS = ["console", "widescreen", "standard", "tablet", "mobile", "preview"]

DEFAULT_CONFIG = {
    # Wallpapers go in a subfolder named after the resolution, e.g. "...\Xbox Wallpapers\1920x1080".
    "wallpaper_dir": str(Path.home() / "Pictures" / "Xbox Wallpapers"),
    # Only wallpapers offered in exactly this size are downloaded.
    "resolution": "1920x1080",
    # How often the desktop wallpaper changes.
    "interval_minutes": 60,
    "shuffle": True,
    # "fill", "fit", "stretch", "center", "span"
    "wallpaper_style": "fill",
    "lock_screen": {
        "enabled": True,
        # true: lock screen shows the same wallpaper as the desktop; false: a different random one.
        "same_as_desktop": True,
    },
    # Seconds to wait between downloads so we don't hammer the site.
    "delay_seconds": 0.5,
}

log = logging.getLogger("xwp")


# --------------------------------------------------------------------------- utils

def setup_logging(verbose: bool) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty() or verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def load_json(path: Path, default: dict) -> dict:
    try:
        return {**default, **json.loads(path.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(default)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict:
    stored = load_json(CONFIG_PATH, {})
    cfg = {k: stored.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    cfg["lock_screen"] = {**DEFAULT_CONFIG["lock_screen"], **(cfg["lock_screen"] if isinstance(cfg["lock_screen"], dict) else {})}
    if stored != cfg:
        save_json(CONFIG_PATH, cfg)  # add new defaults, drop obsolete settings
    return cfg


def load_state() -> dict:
    # downloads: image URL -> file name; sizes: image URL -> "WxH" learned by probing.
    return load_json(STATE_PATH, {"downloads": {}, "sizes": {}, "last_sync": None, "lock_current": None})


def update_state(**changes) -> None:
    """Merge changes into the state file (sync and the watcher both write it)."""
    state = load_state()
    state.update(changes)
    save_json(STATE_PATH, state)


def slug(text: str, maxlen: int = 120) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f®™©]', "", text).strip(" .")
    return text[:maxlen].rstrip(" .") or "wallpaper"


def parse_resolution(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", str(text))
    if not m:
        raise SystemExit(f'Invalid "resolution" in {CONFIG_PATH}: {text!r} (expected e.g. "1920x1080")')
    return int(m.group(1)), int(m.group(2))


def image_dir(cfg: dict) -> Path:
    w, h = parse_resolution(cfg["resolution"])
    return Path(cfg["wallpaper_dir"]) / f"{w}x{h}"


def list_images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"))


# ------------------------------------------------------------------------- download

def http_get(url: str, byte_range: tuple[int, int] | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(quote(url, safe=":/?=&%#+,;@"), headers={"User-Agent": USER_AGENT})
    if byte_range:
        req.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1]}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_catalog() -> list[dict]:
    """The wallpapers page loads its whole catalog from a JS file: `wallpapers = [ {...}, ... ]`."""
    text = http_get(LIST_URL).decode("utf-8-sig")
    m = re.search(r"wallpapers\s*=\s*(\[.*\])", text, re.S)
    if not m:
        raise RuntimeError(f"Wallpaper list not found in {LIST_URL}; the site layout may have changed.")
    return json.loads(m.group(1))


def image_size(data: bytes) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


SIZE_IN_NAME_RE = re.compile(r"[_-](\d{3,5})x(\d{3,5})\D*$")


def labeled_size(url: str) -> tuple[int, int] | None:
    """Most asset names end in their size, e.g. '...?n=Halo_Wallpaper-Xbox_1920x1080.jpg'."""
    m = SIZE_IN_NAME_RE.search(url.split("?n=")[-1].rsplit(".", 1)[0])
    return (int(m.group(1)), int(m.group(2))) if m else None


def probe_size(url: str, sizes: dict) -> tuple[int, int] | None:
    """Read an image's size from its first 64 KB (older assets don't have it in the name)."""
    if url not in sizes:
        try:
            size = image_size(http_get(url, byte_range=(0, 65535)))
        except Exception as e:
            log.warning("could not check size of %s: %s", url, e)
            return None  # not cached: retried next sync
        sizes[url] = f"{size[0]}x{size[1]}" if size else None
    return parse_resolution(sizes[url]) if sizes[url] else None


def pick_variant(wp: dict, wanted: tuple[int, int], sizes: dict) -> str | None:
    """Return the URL of this wallpaper's variant in exactly the wanted size, or None."""
    urls = [ASSET_BASE + wp[k] for k in VARIANTS if isinstance(wp.get(k), str) and wp[k].startswith("/")]
    for url in urls:
        if labeled_size(url) == wanted:
            return url
    for url in urls:
        if labeled_size(url) is None and probe_size(url, sizes) == wanted:
            return url
    return None


def unique_path(folder: Path, name: str, ext: str) -> Path:
    path = folder / f"{name}{ext}"
    n = 2
    while path.exists():
        path = folder / f"{name} ({n}){ext}"
        n += 1
    return path


# ---------------------------------------------------------------------- Windows APIs

POSITIONS = {"center": 0, "tile": 1, "stretch": 2, "fit": 3, "fill": 4, "span": 5}
THEME_STYLES = {"center": ("0", "0"), "tile": ("0", "1"), "stretch": ("2", "0"), "fit": ("6", "0"),
                "fill": ("10", "0"), "span": ("22", "0")}
DSO_SHUFFLEIMAGES, DSS_SLIDESHOW, DSD_FORWARD = 1, 2, 0

_dw = None


def desktop_wallpaper():
    """The IDesktopWallpaper COM object: what Settings and the desktop's right-click menu use."""
    global _dw
    if _dw is None:
        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
        from ctypes import POINTER, c_int, c_void_p
        LPCWSTR, UINT, DWORD, BOOL = wintypes.LPCWSTR, wintypes.UINT, wintypes.DWORD, wintypes.BOOL

        class IDesktopWallpaper(IUnknown):
            _iid_ = GUID("{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}")
            _methods_ = [
                COMMETHOD([], HRESULT, "SetWallpaper", (["in"], LPCWSTR), (["in"], LPCWSTR)),
                COMMETHOD([], HRESULT, "GetWallpaper", (["in"], LPCWSTR), (["out"], POINTER(c_void_p))),
                COMMETHOD([], HRESULT, "GetMonitorDevicePathAt", (["in"], UINT), (["out"], POINTER(c_void_p))),
                COMMETHOD([], HRESULT, "GetMonitorDevicePathCount", (["out"], POINTER(UINT))),
                COMMETHOD([], HRESULT, "GetMonitorRECT", (["in"], LPCWSTR), (["out"], POINTER(wintypes.RECT))),
                COMMETHOD([], HRESULT, "SetBackgroundColor", (["in"], DWORD)),
                COMMETHOD([], HRESULT, "GetBackgroundColor", (["out"], POINTER(DWORD))),
                COMMETHOD([], HRESULT, "SetPosition", (["in"], c_int)),
                COMMETHOD([], HRESULT, "GetPosition", (["out"], POINTER(c_int))),
                COMMETHOD([], HRESULT, "SetSlideshow", (["in"], POINTER(IUnknown))),
                COMMETHOD([], HRESULT, "GetSlideshow", (["out"], POINTER(POINTER(IUnknown)))),
                COMMETHOD([], HRESULT, "SetSlideshowOptions", (["in"], c_int), (["in"], UINT)),
                COMMETHOD([], HRESULT, "GetSlideshowOptions", (["out"], POINTER(c_int)), (["out"], POINTER(UINT))),
                COMMETHOD([], HRESULT, "AdvanceSlideshow", (["in"], LPCWSTR), (["in"], c_int)),
                COMMETHOD([], HRESULT, "GetStatus", (["out"], POINTER(DWORD))),
                COMMETHOD([], HRESULT, "Enable", (["in"], BOOL)),
            ]

        comtypes.CoInitialize()
        _dw = comtypes.CoCreateInstance(GUID("{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}"),
                                        interface=IDesktopWallpaper, clsctx=comtypes.CLSCTX_ALL)
    return _dw


def _take_com_string(ptr) -> str:
    s = ctypes.wstring_at(ptr) if ptr else ""
    ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(ptr))
    return s


def current_wallpaper() -> str:
    """Image currently shown on the first monitor (also correct while a slideshow runs)."""
    dw = desktop_wallpaper()
    try:
        monitor = _take_com_string(dw.GetMonitorDevicePathAt(0))
        return _take_com_string(dw.GetWallpaper(monitor))
    except OSError:
        return ""


def slideshow_running() -> bool:
    try:
        return bool(desktop_wallpaper().GetStatus() & DSS_SLIDESHOW)
    except OSError:
        return False


def in_folder(path: str, folder: Path) -> bool:
    return bool(path) and os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(str(folder.resolve()))


def theme_active(cfg: dict) -> bool:
    return in_folder(current_wallpaper(), image_dir(cfg))


def set_slideshow_options(cfg: dict) -> None:
    dw = desktop_wallpaper()
    dw.SetPosition(POSITIONS.get(cfg["wallpaper_style"], POSITIONS["fill"]))
    dw.SetSlideshowOptions(DSO_SHUFFLEIMAGES if cfg["shuffle"] else 0, interval_ms(cfg))


def start_slideshow(folder: Path, cfg: dict) -> None:
    """Point the Windows desktop slideshow at the folder, silently (no Settings window)."""
    from comtypes import GUID, IUnknown
    from ctypes import POINTER
    shell32 = ctypes.windll.shell32
    item, items = POINTER(IUnknown)(), POINTER(IUnknown)()
    iid_item, iid_array = GUID("{43826d1e-e718-42ee-bc55-a1e261c37bfe}"), GUID("{b63ea76d-1f85-456f-a19c-48159efa858b}")
    hr = shell32.SHCreateItemFromParsingName(str(folder), None, byref(iid_item), byref(item))
    if hr:
        raise ctypes.WinError(hr)
    hr = shell32.SHCreateShellItemArrayFromShellItem(item, byref(iid_array), byref(items))
    if hr:
        raise ctypes.WinError(hr)
    set_slideshow_options(cfg)
    dw = desktop_wallpaper()
    dw.SetSlideshow(items)
    # Otherwise the old wallpaper stays until the first tick. Windows refuses (E_UNEXPECTED)
    # until it has finished reading a big folder, so retry for a while.
    for _ in range(30):
        time.sleep(1)
        try:
            dw.AdvanceSlideshow(None, DSD_FORWARD)
            break
        except OSError:
            pass
    time.sleep(1)


def interval_ms(cfg: dict) -> int:
    return int(max(1, float(cfg["interval_minutes"])) * 60_000)


def write_theme(cfg: dict) -> None:
    """Install the theme file, so "Xbox Wallpapers" is listed in Settings > Personalization > Themes."""
    folder = image_dir(cfg)
    images = list_images(folder)
    style, tile = THEME_STYLES.get(cfg["wallpaper_style"], THEME_STYLES["fill"])
    text = "\n".join([
        "; Generated by Xbox Wallpaper Changer (xwp apply). Edits are overwritten.",
        "[Theme]",
        f"DisplayName={THEME_NAME}",
        "",
        r"[Control Panel\Desktop]",
        f"Wallpaper={random.choice(images) if images else ''}",
        f"TileWallpaper={tile}",
        f"WallpaperStyle={style}",
        "Pattern=",
        "",
        "[Slideshow]",
        f"Interval={interval_ms(cfg)}",
        f"Shuffle={1 if cfg['shuffle'] else 0}",
        f"ImagesRootPath={folder}",
        "",
        "[VisualStyles]",
        r"Path=%SystemRoot%\resources\themes\Aero\Aero.msstyles",
        "ColorStyle=NormalColor",
        "Size=NormalSize",
        "AutoColorization=0",
        "ColorizationColor=0XC4107C10",
        *color_mode_lines(),
        "",
        "[MasterThemeSelector]",
        "MTSM=RJSPBS",
        "",
    ])
    THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = THEME_PATH.read_text(encoding="mbcs")
    except OSError:
        old = ""
    if not_wallpaper(old) != not_wallpaper(text):  # keep the file as is if only the start image would differ
        THEME_PATH.write_text(text, encoding="mbcs", errors="replace")  # Windows ignores UTF-16/UTF-8 .theme files


def not_wallpaper(theme_text: str) -> str:
    return re.sub(r"(?m)^Wallpaper=.*$", "", theme_text)


def color_mode_lines() -> list[str]:
    """Keep the current light/dark mode: a theme without these switches Windows to light."""
    import winreg
    lines = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            for name, value in (("SystemMode", "SystemUsesLightTheme"), ("AppMode", "AppsUseLightTheme")):
                light = winreg.QueryValueEx(k, value)[0]
                lines.append(f"{name}={'Light' if light else 'Dark'}")
    except OSError:
        pass
    return lines


def set_lock_screen(path: Path) -> None:
    import asyncio
    from winrt.windows.storage import StorageFile
    from winrt.windows.system.userprofile import LockScreen

    async def run():
        f = await StorageFile.get_file_from_path_async(str(path))
        await LockScreen.set_image_file_async(f)
    asyncio.run(run())


def sync_lock_screen(cfg: dict, desktop: str, force: bool = False) -> None:
    """Show the desktop's wallpaper (or another random one) on the lock screen."""
    lock = cfg["lock_screen"]
    if not lock["enabled"] or not in_folder(desktop, image_dir(cfg)):
        return
    state = load_state()
    if lock["same_as_desktop"]:
        choice = Path(desktop)
    else:
        skip = {os.path.normcase(desktop), os.path.normcase(state.get("lock_current") or "")}
        others = [p for p in list_images(image_dir(cfg)) if os.path.normcase(str(p)) not in skip]
        if not others:
            return
        choice = random.choice(others)
    if not force and state.get("lock_current") == str(choice):
        return
    try:
        set_lock_screen(choice)
    except Exception as e:
        log.warning("could not set the lock screen to %s: %s", choice.name, e)
        return
    update_state(lock_current=str(choice))
    log.info("Lock screen set: %s", choice.name)


# ---------------------------------------------------------------------- commands

def cmd_sync(args, cfg) -> None:
    wanted = parse_resolution(cfg["resolution"])
    folder = image_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    state = load_state()
    downloads, sizes = state["downloads"], state["sizes"]

    catalog = fetch_catalog()
    log.info("%d wallpapers listed on xbox.com, looking for %dx%d.", len(catalog), *wanted)
    added = no_size = 0
    for wp in catalog[:args.limit or None]:
        title = slug(wp.get("title") or "")
        url = pick_variant(wp, wanted, sizes)
        if not url:
            no_size += 1
            continue
        if url in downloads:
            continue  # already downloaded (if you deleted the file, it stays deleted)
        if args.dry_run:
            log.info("[dry-run] would download: %s", title)
            continue
        try:
            data = http_get(url, timeout=120)
        except Exception as e:
            log.warning("download failed for %s: %s", title, e)
            continue
        size = image_size(data)
        if size != wanted:
            log.info("skip (actual size %s, not %dx%d): %s", size, *wanted, title)
            sizes[url] = f"{size[0]}x{size[1]}" if size else None
            downloads[url] = None
            continue
        path = unique_path(folder, title, ".png" if data[:4] == b"\x89PNG" else ".jpg")
        path.write_bytes(data)
        downloads[url] = path.name
        added += 1
        log.info("downloaded: %s", path.name)
        update_state(downloads=downloads, sizes=sizes)
        time.sleep(cfg["delay_seconds"])

    if not args.dry_run:
        update_state(last_sync=time.strftime("%Y-%m-%d %H:%M:%S"))
    update_state(downloads=downloads, sizes=sizes)
    log.info("Sync done: %d new wallpaper(s) in %s; %d not offered in %dx%d.", added, folder, no_size, *wanted)
    if added and not args.dry_run:
        write_theme(cfg)
        if theme_active(cfg) and slideshow_running():
            start_slideshow(folder, cfg)  # so the running slideshow picks up the new files


def cmd_apply(args, cfg) -> None:
    folder = image_dir(cfg)
    if not list_images(folder):
        raise SystemExit(f"No wallpapers in {folder} yet - run: xwp sync")
    write_theme(cfg)
    start_slideshow(folder, cfg)
    log.info('Theme "%s" applied (%s min, shuffle %s). Wallpapers: %s',
             THEME_NAME, cfg["interval_minutes"], "on" if cfg["shuffle"] else "off", folder)
    sync_lock_screen(cfg, current_wallpaper(), force=True)


def cmd_next(args, cfg) -> None:
    if not (theme_active(cfg) and slideshow_running()):
        log.info('The "%s" theme isn\'t active - applying it.', THEME_NAME)
        return cmd_apply(args, cfg)
    desktop_wallpaper().AdvanceSlideshow(None, DSD_FORWARD)
    time.sleep(1)
    cur = current_wallpaper()
    log.info("Wallpaper set: %s", Path(cur).name)
    sync_lock_screen(cfg, cur)


def cmd_lock(args, cfg) -> None:
    cur = current_wallpaper()
    if not in_folder(cur, image_dir(cfg)):
        log.info('The desktop isn\'t showing an Xbox wallpaper (theme not active) - lock screen left as is.')
    elif not cfg["lock_screen"]["enabled"]:
        log.info("Lock screen sync is disabled in the config.")
    else:
        sync_lock_screen(cfg, cur, force=True)


def cmd_watch(args, cfg) -> None:
    """Wait for wallpaper changes (Windows slideshow, right-click > Next, Settings) and follow them.

    Uses no CPU while waiting: it sleeps until Windows rewrites its wallpaper cache in
    %APPDATA%\\Microsoft\\Windows\\Themes, which it does on every wallpaper change, with a
    once-a-minute check as a fallback (that also picks up config edits).
    """
    lock = open(APP_DIR / "watch.lock", "a+b")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("watch is already running.")
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    kernel32.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    FILE_NOTIFY_CHANGE_FILE_NAME, FILE_NOTIFY_CHANGE_LAST_WRITE, WAIT_OBJECT_0 = 0x1, 0x10, 0
    themes_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    change = kernel32.FindFirstChangeNotificationW(
        str(themes_dir), True, FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE)
    if change in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError()
    log.info("Watching for wallpaper changes.")

    cfg_mtime, last = None, None
    while True:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime != cfg_mtime:
            old, cfg = cfg, load_config()
            if cfg_mtime is not None:
                log.info("Config changed - reapplying.")
                write_theme(cfg)
                if image_dir(old) != image_dir(cfg):
                    # e.g. a new resolution: move the running theme to the new folder
                    if in_folder(current_wallpaper(), image_dir(old)) and list_images(image_dir(cfg)):
                        start_slideshow(image_dir(cfg), cfg)
                elif theme_active(cfg) and slideshow_running():
                    set_slideshow_options(cfg)
                last = None  # lock screen settings may have changed too
            cfg_mtime = mtime
        cur = current_wallpaper()
        if cur and cur != last:
            last = cur
            sync_lock_screen(cfg, cur)
        if kernel32.WaitForSingleObject(change, 60_000) == WAIT_OBJECT_0:
            time.sleep(1)  # Windows writes several files per change; let it finish
            kernel32.FindNextChangeNotification(change)


def cmd_status(args, cfg) -> None:
    state = load_state()
    folder = image_dir(cfg)
    active = theme_active(cfg)
    print(f"Wallpapers:   {len(list_images(folder))} in {folder}")
    print(f"Theme:        {THEME_PATH} ({'active' if active else 'not active'})")
    print(f"Slideshow:    {'running' if slideshow_running() else 'off'}, every {cfg['interval_minutes']} min")
    print(f"Current:      {current_wallpaper()}")
    lock = cfg["lock_screen"]
    print(f"Lock screen:  {'off' if not lock['enabled'] else 'same as desktop' if lock['same_as_desktop'] else 'different random'}"
          f" - last set {state.get('lock_current')}")
    print(f"Last sync:    {state.get('last_sync')}")
    print(f"Config:       {CONFIG_PATH}")
    print(f"Log:          {LOG_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="xwp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true", help="show what would be downloaded, change nothing")
    s.add_argument("--limit", type=int, help="only look at the first N wallpapers listed (for testing)")
    for name in ("apply", "next", "lock", "watch", "status"):
        sub.add_parser(name)
    args = ap.parse_args()
    setup_logging(args.verbose)
    cfg = load_config()
    commands = {"sync": cmd_sync, "apply": cmd_apply, "next": cmd_next, "lock": cmd_lock,
                "watch": cmd_watch, "status": cmd_status}
    try:
        commands[args.cmd](args, cfg)
    except SystemExit:
        raise
    except Exception:
        log.exception("%s failed", args.cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
