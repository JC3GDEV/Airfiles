#!/usr/bin/env python3
"""Airfiles - receive files on the M5Stack CardputerZero.

Receive paths
  * Web (works with iPhone, Android, Windows, macOS, Linux, curl):
    a small upload server on the local Wi-Fi/LAN. The screen shows a QR code
    and a PIN; open the link on the phone, pick files, done.
  * AirDrop (experimental, off by default): runs OWL + OpenDrop when they are
    installed and a Wi-Fi adapter that supports monitor mode is present.

Screen: 320x170 framebuffer (Pillow -> RGB565 on /dev/fb0), keyboard via evdev.

Usage
  airfiles.py               run on the device
  airfiles.py --headless    server only (no screen/keyboard), e.g. over SSH
  airfiles.py --preview out.png [home|active|inbox|port|files|menu|share|pick|sending]
                            render a screen to a PNG
"""
import argparse
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from PIL import Image, ImageDraw, ImageFont

VERSION = "0.3.0"
W, H = 320, 170

BG = (8, 12, 20)
BAR = (20, 28, 44)
FG = (230, 235, 245)
DIM = (125, 135, 155)
ACC = (80, 200, 255)
OK = (90, 220, 140)
WARN = (255, 190, 80)
SEL = (30, 50, 80)


# ----------------------------------------------------------------- config
def config_path():
    return Path(os.environ.get("AIRFILES_CONFIG",
                               Path.home() / ".config/airfiles/config.json"))


def save_config_value(key, value):
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data[key] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError:
        pass


def load_config():
    cfg = {
        "port": 8088,
        "inbox": str(Path.home() / "Airfiles"),
        "pin": True,                 # require a PIN for the web page
        "max_mb": 2048,              # per-file limit
        "airdrop": False,            # start OWL + OpenDrop at launch
        "airdrop_iface": "wlan1",    # Wi-Fi adapter OWL may take over
        "fb": "/dev/fb0",
        "rotate": 0,                 # 0/90/180/270 if the panel is rotated
        "browse_root": "~",          # top folder the Files browser may open
        "share_minutes": 10,         # how long a download link stays valid
        "exit_hold_s": 3.0,          # hold Esc this long to exit
        "esc_keys": ["KEY_ESC", "KEY_GRAVE"],   # keys that act as Esc
    }
    path = config_path()
    try:
        cfg.update(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


# ------------------------------------------------------------------ state
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.inbox = Path(cfg["inbox"]).expanduser()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.browse_root = Path(cfg["browse_root"]).expanduser().resolve()
        if not self.browse_root.is_dir():
            self.browse_root = self.inbox.resolve()
        home = Path.home().resolve()
        self.browse_label = "~" if self.browse_root == home else str(self.browse_root)
        self.shares = {}             # token -> {"path", "until"} for download links
        self.port = int(cfg["port"])
        self.ip = ""
        self.pin = new_pin() if cfg["pin"] else ""
        self.files = []              # [(name, size, mtime)] newest first
        self.active = {}             # id -> {name, done, total, t0}
        self.airdrop = "off"
        self.toast = ("", 0.0)
        self.error = ""
        self.srv = None              # running HTTP server, or None if the port is busy
        self.fails = {}              # client ip -> recent wrong-PIN times
        self.esc_since = None        # monotonic time Esc went down, or None
        self.lock = threading.Lock()
        self.dirty = threading.Event()

    def url(self):
        if not self.ip:
            return ""
        u = f"http://{self.ip}:{self.port}/"
        return u + (f"?pin={self.pin}" if self.pin else "")

    def share_link(self, path):
        """Create a time-limited download link for one file (QR-friendly: short)."""
        now = time.monotonic()
        self.shares = {k: v for k, v in self.shares.items() if v["until"] > now}
        token = secrets.token_urlsafe(6)
        until = now + 60 * float(self.cfg["share_minutes"])
        self.shares[token] = {"path": str(path), "until": until}
        url = f"http://{self.ip}:{self.port}/dl/{token}"
        return url + (f"?pin={self.pin}" if self.pin else ""), until

    def set_port(self, port):
        """Move the web server to `port` and remember it. The new socket is
        opened first, so a busy port leaves the old server untouched."""
        try:
            new = start_server(self, port)
        except OSError:
            return False
        old, self.srv = self.srv, new
        self.port = port
        self.error = ""
        if old:
            old.shutdown()
            old.server_close()
        save_config_value("port", port)
        self.say(f"Port set to {port}")
        return True

    def say(self, text, secs=4):
        self.toast = (text, time.time() + secs)
        self.dirty.set()


def new_pin():
    return f"{secrets.randbelow(10000):04d}"


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


SAFE = re.compile(r"[^\w.\- ()\[\]]+", re.U)


def safe_name(name):
    name = unquote(name or "").replace("\\", "/")
    name = os.path.basename(name)
    name = SAFE.sub("_", name).strip(" .")
    return name[:120] or "file"


def publish(tmp, inbox, name):
    """Move tmp into inbox under a unique name without overwriting."""
    p = Path(name)
    stem, suf = p.stem, p.suffix
    n = 0
    while True:
        target = inbox / (name if n == 0 else f"{stem}-{n}{suf}")
        try:
            os.link(tmp, target)
            os.unlink(tmp)
            return target
        except FileExistsError:
            n += 1


def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def age(ts):
    d = max(0, time.time() - ts)
    if d < 90:
        return f"{int(d)}s"
    if d < 5400:
        return f"{int(d / 60)}m"
    if d < 129600:
        return f"{int(d / 3600)}h"
    return f"{int(d / 86400)}d"


def clip(s, n):
    return s if len(s) <= n else s[: max(1, n - 4)] + "\u2026" + s[-3:]


# ------------------------------------------------------------------- web
PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Airfiles</title>
<style>
body{font:16px system-ui,sans-serif;background:#0c1220;color:#e6ebf5;margin:0;padding:24px}
h1{margin:0 0 4px}p{color:#8a95ad;margin:0 0 20px}
label{display:block;border:2px dashed #3a4a6b;border-radius:14px;padding:36px 12px;
text-align:center;cursor:pointer}
h2{font-size:17px;margin:26px 0 6px}#dev div{padding:6px 0;word-break:break-all}
input{display:none}#log div{padding:8px 0;border-bottom:1px solid #1d2842;word-break:break-all}
</style>
<h1>Airfiles</h1><p>Send files to your Cardputer</p>
<label for=f>Tap to choose files<input id=f type=file multiple></label>
<div id=log></div>
<h2>On the device</h2><div id=dev></div>
<script>
const pin=new URLSearchParams(location.search).get('pin')||'';
const MAXB=__MAXB__;
const f=document.getElementById('f'),log=document.getElementById('log');
f.onchange=async()=>{for(const file of f.files){await up(file)}f.value='';list()};
async function list(){try{
const r=await fetch('/files'+(pin?'?pin='+encodeURIComponent(pin):''));const a=await r.json();
const box=document.getElementById('dev');box.textContent='';
for(const x of a.slice(0,50)){const d=document.createElement('div');const l=document.createElement('a');
l.href='/files/'+encodeURIComponent(x.name)+(pin?'?pin='+encodeURIComponent(pin):'');
l.textContent=x.name;l.style.color='#6cc8ff';
d.append(l,document.createTextNode('  '+Math.max(1,Math.round(x.size/1024))+' KB'));box.append(d)}
}catch(e){}}
list();
function up(file){return new Promise(res=>{
const row=document.createElement('div');row.textContent=file.name+' ...';log.prepend(row);
if(file.size>MAXB){row.textContent=file.name+'  too large (limit '+Math.round(MAXB/1048576)+' MB)';return res()}
const x=new XMLHttpRequest();
x.open('PUT','/upload/'+encodeURIComponent(file.name)+(pin?'?pin='+encodeURIComponent(pin):''));
x.upload.onprogress=e=>{if(e.lengthComputable)row.textContent=file.name+'  '+Math.round(e.loaded/e.total*100)+'%'};
x.onload=()=>{row.textContent=file.name+(x.status==200?'  done':'  failed: '+x.responseText);res()};
x.onerror=()=>{row.textContent=file.name+'  network error';res()};
x.send(file)})}
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Airfiles/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    # ---- helpers
    def _reply(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self):
        """PIN check. 10 wrong PINs from one address lock it out for 60 s."""
        if not self.app.pin:
            return True
        ip, now = self.client_address[0], time.monotonic()
        fails = [t for t in self.app.fails.get(ip, []) if now - t < 60]
        if len(self.app.fails) > 1000:
            self.app.fails.clear()
        if len(fails) >= 10:
            self.app.fails[ip] = fails
            return False
        given = parse_qs(urlparse(self.path).query).get("pin", [""])[0]
        given = given or self.headers.get("X-Pin", "")
        ok = secrets.compare_digest(given.encode(), self.app.pin.encode())
        if not ok:
            fails.append(now)
        self.app.fails[ip] = fails
        return ok

    def _declared_length(self):
        try:
            return max(0, int(self.headers.get("Content-Length", "0")))
        except ValueError:
            return 0

    def _reject(self, code, message, drain_cap=8 * 1024 * 1024):
        """Refuse an upload. A small body is read and discarded first so the
        client receives a clean HTTP error instead of a reset connection."""
        self.close_connection = True
        left = min(self._declared_length(), drain_cap)
        try:
            self.connection.settimeout(5)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        self._reply(code, message, extra={"Connection": "close"})

    # ---- GET
    def do_GET(self):
        if not self._authed():
            return self._reply(401, "PIN required (add ?pin=1234)")
        path = urlparse(self.path).path
        if path == "/":
            page = PAGE.replace("__MAXB__", str(int(self.app.cfg["max_mb"]) * 1048576))
            return self._reply(200, page, "text/html; charset=utf-8")
        if path == "/files":
            data = [{"name": n, "size": s} for n, s, _ in self.app.files]
            return self._reply(200, json.dumps(data), "application/json")
        if path.startswith("/files/"):
            name = safe_name(path[len("/files/"):])
            target = self.app.inbox / name
            if name.startswith(".") or not target.is_file():
                return self._reply(404, "not found")
            return self._send_file(target, name)
        if path.startswith("/dl/"):
            share = self.app.shares.get(path[4:].split("/")[0])
            if not share or share["until"] < time.monotonic():
                return self._reply(404, "link expired or unknown")
            target = Path(share["path"])
            if not target.is_file() or not (in_dir(target, self.app.browse_root)
                                            or in_dir(target, self.app.inbox)):
                return self._reply(404, "not found")
            return self._send_file(target, target.name)
        self._reply(404, "not found")

    def _send_file(self, target, name):
        try:
            f = open(target, "rb")
            size = os.fstat(f.fileno()).st_size
        except OSError:
            return self._reply(404, "not found")
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    shutil.copyfileobj(f, self.wfile, 65536)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

    do_HEAD = do_GET

    # ---- upload (PUT or POST)
    def do_PUT(self):
        if not self._authed():
            return self._reject(401, "PIN required")
        url = urlparse(self.path)
        if not (url.path == "/upload" or url.path.startswith("/upload/")):
            return self._reject(404, "not found")

        raw = url.path[len("/upload/"):] if url.path.startswith("/upload/") else ""
        raw = raw or parse_qs(url.query).get("name", [""])[0] \
            or self.headers.get("X-Filename", "")
        name = safe_name(raw) if raw else time.strftime("upload-%Y%m%d-%H%M%S")

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self._reply(411, "Content-Length required")
        limit = int(self.app.cfg["max_mb"]) * 1024 * 1024
        if length > limit:
            return self._reject(413, f"file larger than {self.app.cfg['max_mb']} MB")
        if shutil.disk_usage(self.app.inbox).free < length + 50 * 1024 * 1024:
            return self._reject(507, "not enough space on the device")

        tid = secrets.token_hex(4)
        tmp = self.app.inbox / f".{secrets.token_hex(6)}.part"
        job = {"name": name, "done": 0, "total": length, "t0": time.time()}
        self.app.active[tid] = job
        self.app.dirty.set()
        last = 0.0
        try:
            with open(tmp, "wb") as f:
                left = length
                while left:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        raise ConnectionError("client closed early")
                    f.write(chunk)
                    left -= len(chunk)
                    job["done"] += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.2:
                        last = now
                        self.app.dirty.set()
            ctype = self.headers.get("Content-Type", "")
            if ctype.lower().startswith("multipart/form-data"):
                name = self._unwrap_multipart(tmp, ctype, name)
            final = publish(tmp, self.app.inbox, name)
        except ValueError as e:
            tmp.unlink(missing_ok=True)
            self.close_connection = True
            return self._reply(400, str(e))
        except Exception:
            tmp.unlink(missing_ok=True)
            self.close_connection = True
            return
        finally:
            self.app.active.pop(tid, None)
            self.app.dirty.set()
        self.app.say(f"Received {final.name}")
        self._reply(200, json.dumps({"saved": final.name}), "application/json")

    do_POST = do_PUT

    @staticmethod
    def _unwrap_multipart(tmp, ctype, fallback):
        """Single-file multipart/form-data (curl -F, iOS Shortcuts 'Form')."""
        m = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
        if not m:
            raise ValueError("multipart without boundary")
        closing = b"\r\n--" + m.group(1).encode() + b"--"
        size = tmp.stat().st_size
        with open(tmp, "rb") as f:
            head = f.read(16384)
            end = head.find(b"\r\n\r\n")
            if end < 0:
                raise ValueError("bad multipart body")
            start = end + 4
            f.seek(max(0, size - len(closing) - 4))
            tail = f.read()
            idx = tail.rfind(closing)
            if idx < 0:
                raise ValueError("bad multipart body (send one file per request)")
            stop = size - len(tail) + idx
        fm = re.search(rb'filename="([^"]*)"', head[:end])
        name = safe_name(fm.group(1).decode("utf-8", "replace")) if fm else fallback
        out = tmp.with_suffix(".unwrap")
        with open(tmp, "rb") as src, open(out, "wb") as dst:
            src.seek(start)
            left = stop - start
            while left > 0:
                buf = src.read(min(65536, left))
                if not buf:
                    break
                dst.write(buf)
                left -= len(buf)
        os.replace(out, tmp)
        return name


def start_server(app, port=None):
    port = app.port if port is None else port
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.app = app
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- watcher
def scan_inbox(app):
    out = []
    try:
        for p in app.inbox.iterdir():
            if p.name.startswith(".") or not p.is_file():
                continue
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime))
    except OSError:
        pass
    out.sort(key=lambda t: t[2], reverse=True)
    return out[:200]


def watcher(app, stop):
    while not stop.is_set():
        files = scan_inbox(app)
        ip = local_ip()
        if files != app.files or ip != app.ip:
            app.files, app.ip = files, ip
            app.dirty.set()
        stop.wait(1.0)


# ---------------------------------------------------------------- AirDrop
class AirDrop:
    """Experimental: OWL (AWDL) + OpenDrop. Needs a monitor-mode Wi-Fi adapter
    (use a second USB adapter so the built-in Wi-Fi stays on your network) and
    passwordless sudo for owl and pkill, or run Airfiles as root."""

    def __init__(self, app):
        self.app = app
        self.procs = []
        self.log = Path.home() / ".cache/airfiles/airdrop.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.sender = AirDropSender(app)

    def _sudo(self):
        return [] if os.geteuid() == 0 else ["sudo", "-n"]

    def start(self):
        app = self.app
        if not (shutil.which("owl") and shutil.which("opendrop")):
            app.airdrop = "owl/opendrop missing"
        elif not Path(f"/sys/class/net/{app.cfg['airdrop_iface']}").exists():
            app.airdrop = f"no {app.cfg['airdrop_iface']}"
        else:
            logf = open(self.log, "ab")
            pre = self._sudo()
            self.procs.append(subprocess.Popen(
                pre + ["owl", "-i", app.cfg["airdrop_iface"]],
                stdout=logf, stderr=logf, start_new_session=True))
            time.sleep(3)    # let awdl0 come up
            cmd = os.environ.get("AIRFILES_OPENDROP_CMD", "opendrop receive").split()
            self.procs.append(subprocess.Popen(
                cmd, cwd=app.inbox, stdout=logf, stderr=logf,
                start_new_session=True))
            app.airdrop = "listening"
        app.dirty.set()

    def stop(self):
        for pre, pat in (([], "opendrop receive"),
                         (self._sudo(), f"owl -i {self.app.cfg['airdrop_iface']}")):
            subprocess.run(pre + ["pkill", "-f", pat],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.clear()
        self.app.airdrop = "off"
        self.app.dirty.set()

    def toggle(self):
        if self.procs:
            self.stop()
        else:
            self.app.airdrop = "starting"
            self.app.dirty.set()
            threading.Thread(target=self.start, daemon=True).start()


# ------------------------------------------------------------ file browser
MENU_LINK, MENU_AIRDROP, MENU_DELETE = "Download link", "AirDrop send", "Delete"


def in_dir(path, root):
    try:
        path, root = Path(path).resolve(), Path(root).resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def list_dir(path, root):
    """Folders first, then files; hidden files skipped; '..' below the root."""
    rows = []
    try:
        for p in Path(path).iterdir():
            if p.name.startswith("."):
                continue
            if p.is_symlink() and not in_dir(p, root):
                continue                       # a link out of the browse folder
            try:
                st = p.stat()
                isdir = p.is_dir()
                rows.append((p.name, isdir, 0 if isdir else st.st_size, st.st_mtime))
            except OSError:
                continue
    except OSError:
        return []
    rows.sort(key=lambda r: (not r[1], r[0].lower()))
    if Path(path).resolve() != Path(root).resolve():
        rows.insert(0, ("..", True, 0, 0))
    return rows


def refresh_files(app, ui):
    ui.f_entries = list_dir(ui.f_dir, app.browse_root)
    ui.f_sel = min(ui.f_sel, max(0, len(ui.f_entries) - 1))


def open_files(app, ui):
    start = app.inbox.resolve()
    ui.view, ui.fmode, ui.f_sel = "files", "browse", 0
    ui.f_dir = start if in_dir(start, app.browse_root) else app.browse_root
    refresh_files(app, ui)
    if len(ui.f_entries) > 1 and ui.f_entries[0][0] == "..":
        ui.f_sel = 1                           # start on the first real item


def enter_dir(app, ui, path):
    if in_dir(path, app.browse_root):
        ui.f_dir, ui.f_sel = Path(path).resolve(), 0
        refresh_files(app, ui)


def go_up(app, ui):
    here = ui.f_dir
    if here != app.browse_root:
        enter_dir(app, ui, here.parent)
        for i, entry in enumerate(ui.f_entries):
            if entry[0] == here.name:
                ui.f_sel = i
                break


def open_menu(app, ui, path):
    ui.target = Path(path)
    ui.menu_items = [MENU_LINK, MENU_AIRDROP]
    if in_dir(path, app.inbox):
        ui.menu_items.append(MENU_DELETE)
    ui.menu_sel, ui.note, ui.confirm_delete, ui.fmode = 0, "", False, "menu"


def run_menu(app, ui, sender):
    item = ui.menu_items[ui.menu_sel]
    ui.note = ""
    if item != MENU_DELETE:
        ui.confirm_delete = False
    if item == MENU_LINK:
        if not app.srv:
            ui.note = "Web server is off"
        elif not app.ip:
            ui.note = "No network"
        elif not ui.target.is_file():
            ui.note = "File is gone"
        else:
            ui.share_url, ui.share_until = app.share_link(ui.target)
            ui.fmode = "share"
    elif item == MENU_AIRDROP:
        ok, why = sender.ready()
        if ok:
            ui.fmode = "pick"
            sender.discover(ui)
        else:
            ui.note = why
    elif item == MENU_DELETE:
        if not in_dir(ui.target, app.inbox):
            ui.note = "Only inbox files can be deleted"
        elif not ui.confirm_delete:
            ui.confirm_delete = True
        else:
            name = ui.target.name
            ui.target.unlink(missing_ok=True)
            app.files = scan_inbox(app)
            ui.fmode = "browse"
            refresh_files(app, ui)
            app.say(f"Deleted {name}")


def files_back(ui):
    if ui.fmode == "browse":
        ui.view = "home"
        return
    if ui.fmode == "sending" and ui.proc and ui.proc.poll() is None:
        ui.proc.terminate()                    # cancel the transfer
    ui.fmode = "menu" if ui.fmode in ("share", "pick", "sending") else "browse"


def files_key(app, ui, airdrop, action):
    if ui.fmode == "browse":
        count = len(ui.f_entries)
        if action == "up":
            ui.f_sel = max(0, ui.f_sel - 1)
        elif action == "down":
            ui.f_sel = min(max(0, count - 1), ui.f_sel + 1)
        elif action in ("left", "backspace"):
            go_up(app, ui)
        elif action in ("enter", "right") and count:
            name, isdir = ui.f_entries[ui.f_sel][:2]
            if name == "..":
                go_up(app, ui)
            elif isdir:
                enter_dir(app, ui, ui.f_dir / name)
            else:
                open_menu(app, ui, ui.f_dir / name)
    elif ui.fmode == "menu":
        if action == "up":
            ui.menu_sel = max(0, ui.menu_sel - 1)
            ui.confirm_delete = False
        elif action == "down":
            ui.menu_sel = min(len(ui.menu_items) - 1, ui.menu_sel + 1)
            ui.confirm_delete = False
        elif action == "enter":
            run_menu(app, ui, airdrop.sender)
    elif ui.fmode == "pick":
        if action == "up":
            ui.ad_sel = max(0, ui.ad_sel - 1)
        elif action == "down":
            ui.ad_sel = min(max(0, len(ui.ad_receivers) - 1), ui.ad_sel + 1)
        elif action == "enter" and ui.ad_receivers:
            rid, rname = ui.ad_receivers[ui.ad_sel]
            airdrop.sender.send(ui, rid, rname, ui.target)


FOUND = re.compile(r"Found\s+index\s+(\d+)\s+ID\s+(\S+)\s+name\s+(.+?)\s*$")


def parse_found(line):
    """'Found  index 0  ID eccb2f2dcfe7  name John's iPhone' -> (id, name)."""
    m = FOUND.search(line)
    return (m.group(2), m.group(3)) if m else None


class AirDropSender:
    """Send one file to a nearby Apple device with `opendrop find` and
    `opendrop send`. Experimental: needs the AirDrop backend (OWL) running, and
    the phone set to receive from Everyone with AirDrop open."""

    def __init__(self, app):
        self.app = app
        self.log = Path.home() / ".cache/airfiles/airdrop.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def ready(self):
        if not shutil.which("opendrop"):
            return False, "opendrop is not installed"
        if self.app.airdrop != "listening":
            return False, "Press a to start AirDrop first"
        return True, ""

    @staticmethod
    def _popen(cmd, **kw):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env, **kw)

    @staticmethod
    def _stop(proc):
        try:                                   # `opendrop find` stops on Enter
            if proc.stdin:
                proc.stdin.write("\n")
                proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass

    def discover(self, ui, secs=12.0):
        ui.ad_receivers, ui.ad_sel, ui.ad_note, ui.ad_busy = [], 0, "", True
        threading.Thread(target=self._discover, args=(ui, secs), daemon=True).start()

    def _discover(self, ui, secs):
        app = self.app
        try:
            proc = self._popen(["opendrop", "find"], stdin=subprocess.PIPE)
        except OSError as e:
            ui.ad_busy, ui.ad_note = False, f"cannot run opendrop: {e.strerror}"
            app.dirty.set()
            return

        def reader():
            try:
                for line in proc.stdout:
                    found = parse_found(line)
                    if found and found not in ui.ad_receivers:
                        ui.ad_receivers.append(found)
                        app.dirty.set()
            except ValueError:                 # pipe closed underneath us
                pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        end = time.monotonic() + secs
        while time.monotonic() < end and proc.poll() is None and ui.fmode == "pick":
            time.sleep(0.1)
        self._stop(proc)
        reader_thread.join(timeout=2)
        proc.stdout.close()
        ui.ad_busy = False
        if not ui.ad_receivers and ui.fmode == "pick":
            ui.ad_note = "None found. Open AirDrop on the phone (Everyone)"
        app.dirty.set()

    def send(self, ui, rid, rname, path):
        ui.fmode, ui.ad_lines, ui.ad_result, ui.ad_name = "sending", [], "", rname
        threading.Thread(target=self._send, args=(ui, rid, path), daemon=True).start()

    def _send(self, ui, rid, path):
        app = self.app
        try:
            proc = self._popen(["opendrop", "send", "-r", rid, "-f", str(path)],
                               stdin=subprocess.DEVNULL)
        except OSError as e:
            ui.ad_lines, ui.ad_result = [f"cannot run opendrop: {e.strerror}"], "Failed"
            app.dirty.set()
            return
        ui.proc = proc
        seen = []
        with open(self.log, "a") as log:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                seen.append(line.lower())
                log.write(line + "\n")
                log.flush()
                ui.ad_lines = (ui.ad_lines + [line])[-4:]
                app.dirty.set()
        code = proc.wait()
        proc.stdout.close()
        ok = code == 0 and any("successful" in line for line in seen)
        ui.ad_result = "Sent" if ok else "Failed"
        if ok:
            app.say(f"AirDrop sent {Path(path).name}")
        app.dirty.set()


# ------------------------------------------------------------------ screen
_fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
        for base in ("/usr/share/fonts/truetype/dejavu/",):
            try:
                _fonts[key] = ImageFont.truetype(base + name, size)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def qr_matrix(text):
    try:
        import qrcode
        qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        return qr.get_matrix()
    except Exception:
        return None


class UI:
    def __init__(self):
        self.view = "home"
        self.sel = 0
        self.pending_delete = None
        self.port_text = ""
        self.port_fresh = False
        self.port_msg = ""
        # Files browser
        self.fmode = "browse"        # browse | menu | share | pick | sending
        self.f_dir = None
        self.f_sel = 0
        self.f_entries = []
        self.target = None
        self.menu_items = []
        self.menu_sel = 0
        self.confirm_delete = False
        self.note = ""
        self.share_url = ""
        self.share_until = 0.0
        self.proc = None
        # AirDrop send
        self.ad_receivers = []
        self.ad_sel = 0
        self.ad_busy = False
        self.ad_note = ""
        self.ad_lines = []
        self.ad_result = ""
        self.ad_name = ""


def draw_qr(d, text, x, y, size):
    m = qr_matrix(text) if text else None
    d.rectangle((x, y, x + size - 1, y + size - 1), fill=(255, 255, 255))
    if not m:
        d.text((x + size // 2, y + size // 2), "no QR", fill=(0, 0, 0),
               font=font(10), anchor="mm")
        return
    n = len(m)
    scale = max(1, size // (n + 4))
    off = (size - n * scale) // 2
    for r, row in enumerate(m):
        for c, v in enumerate(row):
            if v:
                px, py = x + off + c * scale, y + off + r * scale
                d.rectangle((px, py, px + scale - 1, py + scale - 1), fill=(0, 0, 0))


def draw_exit_overlay(d, app):
    p = esc_progress(app, float(app.cfg["exit_hold_s"]))
    if p < 0.1:
        return
    d.rectangle((30, 62, W - 30, 108), fill=BAR, outline=WARN)
    d.text((W // 2, 77), "Keep holding Esc to exit", fill=FG, font=font(11, True), anchor="mm")
    d.rectangle((44, 92, W - 44, 100), outline=DIM)
    d.rectangle((45, 93, 45 + int((W - 90) * p), 99), fill=WARN)


def draw_menu(d, ui):
    n = len(ui.menu_items)
    h = 32 + n * 16 + (14 if ui.note else 0)
    y0 = max(18, (H - h) // 2)
    d.rectangle((36, y0, W - 36, y0 + h), fill=BAR, outline=ACC)
    d.text((W // 2, y0 + 9), clip(ui.target.name, 30), fill=DIM, font=font(9), anchor="mm")
    for i, item in enumerate(ui.menu_items):
        y = y0 + 20 + i * 16
        label, color = item, FG
        if item == MENU_AIRDROP:
            label = "AirDrop send (beta)"
        if item == MENU_DELETE and ui.confirm_delete:
            label, color = "Enter again to delete", WARN
        if i == ui.menu_sel:
            d.rectangle((40, y - 2, W - 40, y + 12), fill=SEL)
        d.text((48, y), label, fill=color, font=font(11))
    if ui.note:
        d.text((W // 2, y0 + h - 9), clip(ui.note, 38), fill=WARN, font=font(9), anchor="mm")


def render_files(d, app, ui):
    if ui.fmode == "share":
        draw_qr(d, ui.share_url, 186, 22, 130)
        d.text((5, 22), "Scan to download", fill=ACC, font=font(11, True))
        d.text((5, 40), clip(ui.target.name, 28), fill=FG, font=font(10))
        try:
            d.text((5, 54), human(ui.target.stat().st_size), fill=DIM, font=font(10))
        except OSError:
            pass
        left = max(0, int(ui.share_until - time.monotonic()))
        d.text((5, 72), f"Link valid {left // 60}:{left % 60:02d}", fill=DIM, font=font(10))
        d.text((5, 98), "Phone and Cardputer", fill=DIM, font=font(9))
        d.text((5, 109), "must be on the same Wi-Fi", fill=DIM, font=font(9))
        d.text((5, H - 3), "Esc back", fill=DIM, font=font(9), anchor="ls")
        return
    if ui.fmode == "pick":
        d.text((5, 21), "AirDrop to:", fill=ACC, font=font(11, True))
        d.text((W - 5, 22), clip(ui.target.name, 22), fill=DIM, font=font(9), anchor="ra")
        for i, (rid, name) in enumerate(ui.ad_receivers[:7]):
            y = 40 + i * 15
            if i == ui.ad_sel:
                d.rectangle((0, y - 2, W, y + 12), fill=SEL)
            d.text((8, y), clip(name, 36), fill=FG, font=font(11))
        if ui.ad_busy:
            d.text((W - 5, H - 15), "searching...", fill=OK, font=font(9), anchor="rs")
        elif ui.ad_note:
            d.text((5, 60), clip(ui.ad_note, 44), fill=WARN, font=font(9))
        d.text((5, H - 3), "Enter send  Esc back", fill=DIM, font=font(9), anchor="ls")
        return
    if ui.fmode == "sending":
        d.text((5, 21), "AirDrop", fill=ACC, font=font(11, True))
        d.text((5, 38), f"to {clip(ui.ad_name, 30)}", fill=FG, font=font(10))
        d.text((5, 51), clip(ui.target.name, 38), fill=DIM, font=font(9))
        for i, line in enumerate(ui.ad_lines):
            d.text((5, 70 + i * 12), clip(line, 44), fill=FG, font=font(9))
        if ui.ad_result:
            color = OK if ui.ad_result == "Sent" else WARN
            d.text((W // 2, 135), ui.ad_result, fill=color, font=font(16, True), anchor="mm")
        d.text((5, H - 3), "Esc " + ("back" if ui.ad_result else "cancel"),
               fill=DIM, font=font(9), anchor="ls")
        return

    rel = ui.f_dir.relative_to(app.browse_root) if in_dir(ui.f_dir, app.browse_root) else ""
    shown = app.browse_label + ("" if str(rel) in ("", ".") else "/" + str(rel))
    d.text((5, 20), clip(shown, 48), fill=ACC, font=font(9))
    rows = 9
    top = min(max(0, ui.f_sel - rows + 1), max(0, len(ui.f_entries) - rows))
    if not ui.f_entries:
        d.text((W // 2, 85), "Empty folder", fill=DIM, font=font(11), anchor="mm")
    for i, (name, isdir, size, mtime) in enumerate(ui.f_entries[top:top + rows]):
        y = 31 + i * 13
        if top + i == ui.f_sel:
            d.rectangle((0, y - 1, W, y + 11), fill=SEL)
        label = name if name == ".." else (name + "/" if isdir else name)
        d.text((5, y), clip(label, 31), fill=ACC if isdir else FG, font=font(10))
        if not isdir:
            d.text((W - 5, y), f"{human(size)} {age(mtime)}", fill=DIM, font=font(10), anchor="ra")
    d.text((5, H - 3), "Enter open  , up  q back", fill=DIM, font=font(9), anchor="ls")
    d.text((W - 5, H - 3), "hold esc: exit", fill=DIM, font=font(9), anchor="rs")
    if ui.fmode == "menu":
        draw_menu(d, ui)


def render(app, ui):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 16), fill=BAR)
    d.text((5, 8), "AIRFILES", fill=ACC, font=font(11, True), anchor="lm")
    d.text((W - 5, 8), f"{len(app.files)} files  AD:{clip(app.airdrop, 10)}",
           fill=DIM, font=font(9), anchor="rm")

    if ui.view == "files":
        render_files(d, app, ui)
        draw_exit_overlay(d, app)
        return img

    if ui.view == "port":
        d.text((W // 2, 34), "Web server port", fill=DIM, font=font(11), anchor="mm")
        d.rectangle((90, 48, 230, 82), outline=ACC)
        shown = ui.port_text + ("_" if len(ui.port_text) < 5 else "")
        d.text((W // 2, 65), shown, fill=FG, font=font(20, True), anchor="mm")
        d.text((W // 2, 98), "default 8088   range 1024-65535", fill=DIM,
               font=font(9), anchor="mm")
        if ui.port_msg:
            d.text((W // 2, 116), ui.port_msg, fill=WARN, font=font(10, True), anchor="mm")
        d.text((5, H - 3), "0-9 type  Enter save  Esc back", fill=DIM,
               font=font(9), anchor="ls")
        draw_exit_overlay(d, app)
        return img

    if ui.view == "inbox":
        rows = 10
        top = min(max(0, ui.sel - rows + 1), max(0, len(app.files) - rows))
        if not app.files:
            d.text((W // 2, 80), "Inbox is empty", fill=DIM, font=font(11), anchor="mm")
        for i, (name, size, mt) in enumerate(app.files[top:top + rows]):
            y = 19 + i * 13
            if top + i == ui.sel:
                d.rectangle((0, y - 1, W, y + 11), fill=SEL)
            d.text((5, y), clip(name, 31), fill=FG, font=font(10))
            d.text((W - 5, y), f"{human(size)} {age(mt)}", fill=DIM, font=font(10), anchor="ra")
        hint = ("press d again to delete" if ui.pending_delete
                else "up/dn move  d delete  i back")
        d.text((5, H - 3), hint, fill=WARN if ui.pending_delete else DIM,
               font=font(9), anchor="ls")
        d.text((W - 5, H - 3), "hold esc: exit", fill=DIM, font=font(9), anchor="rs")
        draw_exit_overlay(d, app)
        return img

    # ---- home
    draw_qr(d, app.url() if app.srv else "", 186, 22, 130)
    if not app.srv:
        d.text((5, 24), f"Port {app.port} busy", fill=WARN, font=font(13, True))
        d.text((5, 42), "Press p to choose", fill=DIM, font=font(10))
        d.text((5, 54), "a different port", fill=DIM, font=font(10))
    elif app.ip:
        d.text((5, 21), "Open in any browser", fill=DIM, font=font(9))
        d.text((5, 32), f"{app.ip}:{app.port}", fill=FG, font=font(13, True))
        if app.pin:
            d.text((5, 51), "PIN", fill=DIM, font=font(10))
            d.text((34, 47), app.pin, fill=ACC, font=font(20, True))
    else:
        d.text((5, 24), "No network", fill=WARN, font=font(13, True))
        d.text((5, 42), "Join Wi-Fi, then", fill=DIM, font=font(10))
        d.text((5, 54), "this page appears", fill=DIM, font=font(10))
    d.line((5, 78, 176, 78), fill=BAR)

    y = 83
    now = time.time()
    jobs = list(app.active.values())
    if jobs:
        for job in jobs[:2]:
            pct = job["done"] / job["total"] if job["total"] else 0
            speed = job["done"] / max(0.1, now - job["t0"])
            d.text((5, y), clip(job["name"], 15), fill=FG, font=font(10))
            d.text((172, y), f"{int(pct * 100)}% {human(speed)}/s", fill=OK,
                   font=font(9), anchor="ra")
            d.rectangle((5, y + 14, 170, y + 20), outline=DIM)
            d.rectangle((6, y + 15, 6 + int(163 * pct), y + 19), fill=OK)
            y += 28
    else:
        text, until = app.toast
        if text and now < until:
            d.text((5, y), clip(text, 28), fill=OK, font=font(10, True))
            y += 14
        for name, size, mt in app.files[:3 if not (text and now < until) else 2]:
            d.text((5, y), clip(name, 24), fill=FG, font=font(10))
            d.text((176, y), human(size), fill=DIM, font=font(10), anchor="ra")
            y += 13
        if not app.files:
            d.text((5, y), "Waiting for files...", fill=DIM, font=font(10))
    d.text((5, H - 14), "i inbox  f files  p port", fill=DIM, font=font(9), anchor="ls")
    d.text((5, H - 3), "a airdrop  r pin", fill=DIM, font=font(9), anchor="ls")
    d.text((W - 5, H - 3), "hold esc 3s: exit", fill=DIM, font=font(9), anchor="rs")
    draw_exit_overlay(d, app)
    return img


# ---------------------------------------------------------- framebuffer + keys
class Framebuffer:
    def __init__(self, dev, rotate=0):
        import numpy as np
        self.np = np
        sysd = Path("/sys/class/graphics") / Path(dev).name
        self.w, self.h = (int(v) for v in (sysd / "virtual_size").read_text().split(","))
        self.bpp = int((sysd / "bits_per_pixel").read_text())
        self.stride = int((sysd / "stride").read_text())
        self.rotate = rotate
        self.f = open(dev, "r+b", buffering=0)

    def show(self, img):
        np = self.np
        if self.rotate:
            img = img.rotate(self.rotate, expand=True)
        if img.size != (self.w, self.h):
            img = img.resize((self.w, self.h))
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        if self.bpp == 16:
            px = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
            buf = np.zeros((self.h, self.stride // 2), dtype="<u2")
            buf[:, :self.w] = px
        elif self.bpp == 32:
            buf = np.zeros((self.h, self.stride // 4, 4), dtype=np.uint8)
            buf[:, :self.w, 0] = a[..., 2]
            buf[:, :self.w, 1] = a[..., 1]
            buf[:, :self.w, 2] = a[..., 0]
            buf[:, :self.w, 3] = 255
        else:
            raise RuntimeError(f"unsupported framebuffer depth: {self.bpp}")
        self.f.seek(0)
        self.f.write(buf.tobytes())


KEYMAP = {
    "KEY_Q": "quit",
    "KEY_I": "inbox", "KEY_TAB": "inbox",
    "KEY_UP": "up", "KEY_SEMICOLON": "up",
    "KEY_DOWN": "down", "KEY_DOT": "down",
    "KEY_D": "delete", "KEY_DELETE": "backspace", "KEY_BACKSPACE": "backspace",
    "KEY_A": "airdrop", "KEY_R": "pin", "KEY_P": "port",
    "KEY_ENTER": "enter", "KEY_KPENTER": "enter",
    "KEY_F": "files", "KEY_COMMA": "left", "KEY_SLASH": "right",
    "KEY_LEFT": "left", "KEY_RIGHT": "right",
}
for _i in range(10):
    KEYMAP[f"KEY_{_i}"] = f"digit{_i}"
    KEYMAP[f"KEY_KP{_i}"] = f"digit{_i}"


def esc_progress(app, hold, now=None):
    """0.0 (not held) .. 1.0 (held long enough to exit)."""
    if app.esc_since is None:
        return 0.0
    now = time.monotonic() if now is None else now
    return min(1.0, max(0.0, (now - app.esc_since) / hold))


def esc_event(app, q, value, hold, now=None):
    """Feed Esc key events (1=down, 0=up). A short tap sends 'back'; holding
    for `hold` seconds is detected by the main loop via esc_progress()."""
    now = time.monotonic() if now is None else now
    if value == 1 and app.esc_since is None:
        app.esc_since = now
    elif value == 0 and app.esc_since is not None:
        held = now - app.esc_since
        app.esc_since = None
        if held < hold:
            q.put("back")
    app.dirty.set()


def keyboard_thread(q, stop, app=None, esc_names=(), hold=3.0):
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        return
    dev = None
    forced = os.environ.get("AIRFILES_KBD")
    for path in ([forced] if forced else evdev.list_devices()):
        try:
            cand = evdev.InputDevice(path)
            keys = cand.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_ENTER in keys:
                dev = cand
                break
        except OSError:
            continue
    if not dev:
        return
    try:
        dev.grab()           # keep the launcher from reacting to the same keys
    except OSError:
        pass
    try:
        for ev in dev.read_loop():
            if stop.is_set():
                break
            if ev.type == ecodes.EV_KEY and ev.value in (0, 1, 2):
                name = ecodes.KEY.get(ev.code)
                name = name[0] if isinstance(name, list) else name
                if name in esc_names:
                    if app is not None and ev.value in (0, 1):
                        esc_event(app, q, ev.value, hold)
                    continue
                if name in KEYMAP and ev.value and (ev.value == 1 or KEYMAP[name] in ("up", "down", "backspace")):
                    q.put(KEYMAP[name])
    finally:
        try:
            dev.ungrab()
        except OSError:
            pass


def edit_port(app, ui, action):
    if action.startswith("digit"):
        if ui.port_fresh:
            ui.port_text, ui.port_fresh = "", False
        if len(ui.port_text) < 5:
            ui.port_text += action[5:]
        ui.port_msg = ""
    elif action == "backspace":
        ui.port_text = "" if ui.port_fresh else ui.port_text[:-1]
        ui.port_fresh, ui.port_msg = False, ""
    elif action == "enter":
        try:
            n = int(ui.port_text)
        except ValueError:
            ui.port_msg = "type a port number"
            return
        if not 1024 <= n <= 65535:
            ui.port_msg = "use 1024 - 65535"
        elif app.srv is not None and n == app.port:
            ui.view = "home"
        elif app.set_port(n):
            ui.view = "home"
        else:
            ui.port_msg = f"port {n} is busy"


def handle_key(app, ui, airdrop, action):
    """Returns False to quit."""
    if action == "backspace" and ui.view == "inbox":
        action = "delete"
    if action != "delete":
        ui.pending_delete = None
    if action == "back":
        if ui.view == "files":
            files_back(ui)
        else:
            ui.view = "home"
        return True
    if action == "quit":
        if ui.view == "files":
            files_back(ui)
            return True
        if ui.view in ("inbox", "port"):
            ui.view = "home"
            return True
        return False
    if ui.view == "port":
        edit_port(app, ui, action)
        return True
    if ui.view == "files":
        files_key(app, ui, airdrop, action)
        return True
    if action == "files":
        open_files(app, ui)
    elif action == "port":
        ui.view, ui.port_text, ui.port_fresh, ui.port_msg = "port", str(app.port), True, ""
    elif action == "inbox":
        ui.view = "home" if ui.view == "inbox" else "inbox"
    elif action == "up":
        ui.sel = max(0, ui.sel - 1)
    elif action == "down":
        ui.sel = min(max(0, len(app.files) - 1), ui.sel + 1)
    elif action == "airdrop":
        airdrop.toggle()
    elif action == "pin" and app.cfg["pin"]:
        app.pin = new_pin()
    elif action == "delete" and ui.view == "inbox" and app.files:
        name = app.files[min(ui.sel, len(app.files) - 1)][0]
        if ui.pending_delete == name:
            (app.inbox / name).unlink(missing_ok=True)
            ui.pending_delete = None
            app.files = scan_inbox(app)
            ui.sel = min(ui.sel, max(0, len(app.files) - 1))
        else:
            ui.pending_delete = name
    return True


# -------------------------------------------------------------------- main
def demo_state(app, mode):
    now = time.time()
    app.ip, app.port, app.pin = "192.168.1.42", 8088, "4821"
    app.srv = object()
    app.airdrop = "off"
    app.files = [
        ("IMG_2041.HEIC", 3_400_000, now - 40),
        ("Quarterly report.pdf", 1_250_000, now - 600),
        ("firmware-v2.3.bin", 8_900_000, now - 7200),
        ("notes.txt", 2_100, now - 90000),
    ] + [(f"photo-{i:03d}.jpg", 2_000_000 + i * 1000, now - 100000 - i * 500) for i in range(8)]
    if mode == "active":
        app.active = {"a": {"name": "IMG_2043.MOV", "done": 31_000_000,
                            "total": 52_000_000, "t0": now - 4.0}}


def preview_ui(app, mode):
    """Set up demo state and a UI for `mode`, for screenshots and --preview."""
    ui = UI()
    demo_state(app, mode)
    if mode == "inbox":
        ui.view, ui.sel = "inbox", 1
    elif mode == "port":
        ui.view, ui.port_text, ui.port_fresh = "port", "8088", True
    elif mode == "exiting":
        app.esc_since = time.monotonic() - 1.6
    elif mode in ("files", "menu", "share", "pick", "sending"):
        root = Path(tempfile.mkdtemp(prefix="airfiles-demo-"))
        for folder in ("Airfiles", "Documents", "Music", "Pictures"):
            (root / folder).mkdir()
        now = time.time()
        for name, size, ago in (("IMG_2041.HEIC", 3_400_000, 40), ("Quarterly report.pdf", 1_250_000, 600),
                                ("firmware-v2.3.bin", 8_900_000, 7200), ("notes.txt", 2_100, 90000)):
            f = root / "Airfiles" / name
            f.write_bytes(b"")
            os.truncate(f, size)
            os.utime(f, (now - ago, now - ago))
        for name, size, ago in (("Readme.txt", 1_800, 5000), ("backup.zip", 21_000_000, 86400)):
            f = root / name
            f.write_bytes(b"")
            os.truncate(f, size)
            os.utime(f, (now - ago, now - ago))
        app.inbox, app.browse_root, app.browse_label = root / "Airfiles", root, "~"
        open_files(app, ui)
        if mode == "files":
            enter_dir(app, ui, root)
        else:
            open_menu(app, ui, root / "Airfiles" / "IMG_2041.HEIC")
        if mode == "share":
            ui.share_url, ui.share_until = app.share_link(ui.target)
            ui.fmode = "share"
        elif mode == "pick":
            ui.fmode = "pick"
            ui.ad_receivers = [("eccb2f2dcfe7", "John’s iPhone"),
                               ("e63138ac6ba8", "Jane’s MacBook Pro")]
        elif mode == "sending":
            ui.fmode, ui.ad_name = "sending", "John’s iPhone"
            ui.ad_lines = ["Asking receiver to accept ...", "Receiver accepted", "Uploading file ..."]
    return ui


def main():
    ap = argparse.ArgumentParser(description="Airfiles")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--port", type=int, help="web server port (default from config, 8088)")
    ap.add_argument("--preview", nargs="+", metavar=("OUT.png", "VIEW"))
    args = ap.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    if args.preview:
        cfg["inbox"] = "/tmp/airfiles-preview"
        app = App(cfg)
        mode = args.preview[1] if len(args.preview) > 1 else "home"
        render(app, preview_ui(app, mode)).save(args.preview[0])
        return

    app = App(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        app.srv = start_server(app)
    except OSError as e:
        print(f"cannot listen on port {app.port}: {e}", file=sys.stderr)
        if args.headless:
            return 1

    threading.Thread(target=watcher, args=(app, stop), daemon=True).start()
    airdrop = AirDrop(app)
    if cfg["airdrop"]:
        airdrop.toggle()

    if args.headless:
        print(f"Airfiles listening on {app.url() or f'port {app.port}'}  inbox: {app.inbox}")
        stop.wait()
    else:
        fb = Framebuffer(cfg["fb"], int(cfg["rotate"]))
        keys = queue.Queue()
        threading.Thread(
            target=keyboard_thread,
            args=(keys, stop, app, set(cfg["esc_keys"]), float(cfg["exit_hold_s"])),
            daemon=True).start()
        ui = UI()
        running = True
        while running and not stop.is_set():
            busy = app.active or app.toast[1] > time.time()
            app.dirty.wait(0.05 if app.esc_since is not None else (0.25 if busy else 1.0))
            app.dirty.clear()
            while True:
                try:
                    action = keys.get_nowait()
                except queue.Empty:
                    break
                running = handle_key(app, ui, airdrop, action) and running
            if esc_progress(app, float(cfg["exit_hold_s"])) >= 1.0:
                running = False
                break
            if ui.view == "files" and ui.fmode == "browse":
                refresh_files(app, ui)
            fb.show(render(app, ui))
        stop.set()

    if airdrop.procs:
        airdrop.stop()
    if app.srv:
        app.srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
