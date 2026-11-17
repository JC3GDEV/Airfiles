# Changelog

## 0.4.0

- **Rename** in the Files menu, right after AirDrop send: an on-device text editor (Shift for capitals, cursor keys, cursor starts before the extension). Refuses empty names, names with `/`, names starting with a dot and names that already exist; existing download links keep working.
- **Per-file size limit setting:** press `l` to set `max_mb` (1-102400 MB) in the app; it applies at once, is saved, and the upload page checks it too.
- The keyboard thread now sends raw keys and the main loop translates them, so letters can be typed in the editor without triggering commands.

## 0.3.0

- **Files browser** (`f`): browse from your home folder down, hidden files skipped, links that leave the browse folder hidden.
- **Download links:** open a file's menu to get a time-limited link as a QR code (`share_minutes`, default 10); the web page also lists received files for download.
- **AirDrop send (beta):** find nearby Apple devices and send the selected file with OpenDrop; progress, result and cancel on screen.
- Delete inbox files from the Files menu (press Enter twice).
- New settings `browse_root` and `share_minutes`; new keys `f`, `,`/Left and `/`/Right.
- Subprocess pipes are closed after AirDrop scans and sends.

## 0.2.0 - first public release

- Receive files over the local network: QR code + PIN on the 320x170 screen, browser upload page, `curl`/Shortcuts uploads, downloads of received files.
- Wrong-PIN lockout (10 attempts, 60 seconds), filename sanitising, size limit, free-space check, no partial files in the inbox.
- Web port setting in the app (`p`), default 8088, saved to `~/.config/airfiles/config.json`; a busy port is reported without stopping the running server.
- Hold Esc for 3 seconds to exit, with an on-screen progress box.
- Inbox view with delete, new-PIN key, launcher icon, APPLaunch package (`build-deb.sh`).
- Rejected uploads (wrong PIN, too large, no space) now return clean HTTP errors; the web page checks the size limit before uploading.
- Experimental AirDrop via OWL + OpenDrop (off by default, untested on hardware).

## 0.1.0 - first public release
- Initial test release.
- Receive files over the local network: QR code + PIN on the 320x170 screen, browser upload page, `curl`/Shortcuts uploads, downloads of received files.
