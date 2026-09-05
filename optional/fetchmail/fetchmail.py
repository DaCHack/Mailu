#!/usr/bin/env python3

import binascii
from datetime import datetime
import time
import os
from pathlib import Path
from pwd import getpwnam
import tempfile
import shlex
import subprocess
import requests
from socrate import system
import sys
import traceback
from multiprocessing import Process, Event
import re
import threading
import queue
import json
import hashlib
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "testing-20260103-0230"

FETCHMAIL = """
fetchmail -N \
    --idfile {}/.fetchids --uidl \
    --pidfile {}/fetchmail.pid \
    --sslcertck --sslcertpath /etc/ssl/certs \
    {} -f {}
"""


RC_LINE = """
poll "{host}" proto {protocol}  port {port}
    user "{username}" password "{password}"
    is "{user_email}"
    smtphost "{smtphost}"
    {folders}
    {options}
    {lmtp}
"""


def imaputf7encode(s):
    """Encode a string into RFC2060 aka IMAP UTF7"""
    out = ''
    enc = ''
    for c in s.replace('&','&-') + 'X':
        if '\x20' <= c <= '\x7f':
            if enc:
                out += f'&{binascii.b2a_base64(enc.encode("utf-16-be")).rstrip(b"\n=").replace(b"/", b",").decode("ascii")}-'
                enc = ''
            out += c
        else:
            enc += c
    return out[:-1]


def escape_rc_string(arg):
    return "".join("\\x%2x" % ord(char) for char in arg)


# --- New: utilities for controller and worker management ---

def fetch_command(fetchmailhome, fetchmail_custom_options, handler_name):
    return FETCHMAIL.format(fetchmailhome, fetchmailhome, fetchmail_custom_options, shlex.quote(handler_name))


def start_fetchmail_subprocess(fetchmailrc, fetchmailhome, env):
    """Write a temporary fetchmailrc and start fetchmail as a subprocess.Popen instance."""
    handler = tempfile.NamedTemporaryFile(delete=False)
    try:
        handler.write(fetchmailrc.encode("utf8"))
        handler.flush()
        handler.close()
        fetchmail_custom_options = os.environ.get("FETCHMAIL_OPTIONS", "")
        command = fetch_command(fetchmailhome, fetchmail_custom_options, handler.name)
        # Ensure correct $FETCHMAILHOME for secondary environment and start command
        command_env = env.copy()
        command_env["FETCHMAILHOME"] = fetchmailhome
        # Start the fetchmail subprocess so the parent can terminate it when needed
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=command_env)
        return proc, handler.name
    except Exception:
        # Clean up temporary file on error
        try:
            os.unlink(handler.name)
        except Exception:
            pass
        raise


# Keep legacy function name for compatibility but not used by controller directly
def fetchmail(fetchmailrc, fetchmailhome):
    return subprocess.run(fetch_command(fetchmailhome, os.environ.get("FETCHMAIL_OPTIONS", ""), shlex.quote("-")), check=True, capture_output=True, shell=True)


def worker(debug, fetch, fetchmailhome, stop_event: Event):

    # Prepare to trick fetchmail with $FETCHMAILHOME environment variable allowing multiple instances running in parallel (e.g. for IMAP IDLE)
    os.makedirs(fetchmailhome, exist_ok=True)
    fetchids_path = fetchmailhome + "/.fetchids"
    Path(fetchids_path).touch()

    # Make fetchmail user owner of fetchmail home directory and files
    id_fetchmail = getpwnam('fetchmail')
    os.chown(fetchids_path, id_fetchmail.pw_uid, id_fetchmail.pw_gid)
    os.chown(fetchmailhome, id_fetchmail.pw_uid, id_fetchmail.pw_gid)
    os.chmod(fetchids_path, 0o700)
    system.drop_privs_to('fetchmail')

    # Collect options based on system settings and ENV variables
    options = "options antispam 501, 504, 550, 553, 554"
    if "FETCHMAIL_POLL_OPTIONS" in os.environ: options += f' {os.environ["FETCHMAIL_POLL_OPTIONS"]}'
    options += " ssl" if fetch["tls"] else " sslproto ''"
    options += " keep" if fetch["keep"] else " fetchall"

    # Build fetchmailrc
    folders = f"folders {",".join(f'\"{imaputf7encode(item).replace('\"',r"\\34")}\"' for item in fetch["folders"]) or '"INBOX"'}"
    fetchmailrc = RC_LINE.format(
        user_email=escape_rc_string(fetch["user_email"]),
        protocol=fetch["protocol"],
        host=escape_rc_string(fetch["host"]),
        port=fetch["port"],
        smtphost=f'{os.environ["HOSTNAMES"].split(",")[0]}' if fetch['scan'] and os.environ.get('PROXY_PROTOCOL_25', False) else f'{os.environ["FRONT_ADDRESS"]}' if fetch['scan'] else f'{os.environ["FRONT_ADDRESS"]}/2525',
        username=escape_rc_string(fetch["username"]),
        password=escape_rc_string(fetch["password"]),
        options=options,
        folders='' if fetch['protocol'] == 'pop3' else folders,
        lmtp='' if fetch['scan'] else 'lmtp',
    )

    # Identify worker for logs
    user_info = "for %s at %s" % (fetch["username"], fetch["host"])

    # Print debugging messages if required
    if debug:
        print('{} Setting fetchmailrc to {}'.format(time.strftime("%b %d %H:%M:%S"), fetchmailrc))
        print('{} Setting $FETCHMAILHOME to {}'.format(time.strftime("%b %d %H:%M:%S"), fetchmailhome))
        fetchmail_custom_options = os.environ.get("FETCHMAIL_OPTIONS", "")
        print('{} Setting fetchmail command to {}'.format(time.strftime("%b %d %H:%M:%S"), FETCHMAIL.format(fetchmailhome, fetchmailhome, fetchmail_custom_options, "")))

    # Iterate in loop to enable automatic retry on specific error codes
    retry_counter = 2
    time_last_socket_error = datetime.now()

    proc = None
    handler_name = None
    try:
        while not stop_event.is_set() and retry_counter <= 1024:
            print('{} Starting fetchmail {}'.format(time.strftime("%b %d %H:%M:%S"), user_info))
            sys.stdout.flush()
            fetchmail_output = b""
            error_message = ""
            try:
                env = os.environ.copy()
                proc, handler_name = start_fetchmail_subprocess(fetchmailrc, fetchmailhome, env)

                # Monitor subprocess until it exits or stop_event is set
                while True:
                    if stop_event.is_set():
                        # Ask fetchmail to terminate
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        # wait a short time for graceful exit
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        break

                    ret = proc.poll()
                    if ret is not None:
                        # Process finished; capture output
                        out, _ = proc.communicate()
                        fetchmail_output = out or b""
                        error_message = "" if ret == 0 else fetchmail_output.decode('utf8', errors='ignore')
                        break

                    time.sleep(0.5)

                # If we exited the monitor loop because stop_event set, break outer loop
                if stop_event.is_set():
                    break

                # IMAP IDLE Timeout is not an error, but requires fetchmail to restart (thus continue in loop)
                if proc is not None and proc.returncode == 2:
                    print('{} Socket error detected {}. Restart in {} seconds.'.format(time.strftime("%b %d %H:%M:%S"), user_info, retry_counter))
                    sys.stdout.flush()

                    # Reset retry_counter if last error >20min in the past, otherwise increase
                    counter_diff = datetime.now() - time_last_socket_error
                    if counter_diff.total_seconds() > 1200:
                        retry_counter = 2
                    else:
                        retry_counter <<= 1
                    time_last_socket_error = datetime.now()

                    time.sleep(retry_counter)
                    continue

                # No mail is not an error
                if error_message and not error_message.startswith("fetchmail: No mail"):
                    print('{} {}'.format(error_message.rstrip(),user_info))
                    sys.stdout.flush()

            except Exception as error:
                # If an exception happens starting or monitoring the subprocess
                error_message = str(error)
                print('{} Exception in worker: {} {}'.format(time.strftime("%b %d %H:%M:%S"), error_message, user_info))
                sys.stdout.flush()

            finally:
                # Ensure temporary fetchmailrc is removed
                if handler_name and os.path.exists(handler_name):
                    try:
                        os.unlink(handler_name)
                    except Exception:
                        pass

                # Log iteration in database including error message if any
                try:
                    requests.post("http://{}:8080/internal/fetch/{}".format(os.environ['ADMIN_ADDRESS'],fetch['id']),
                        json=(error_message.split('\n')[0] if error_message else ""), timeout=5)
                except Exception:
                    # Avoid crashing the worker if admin is unreachable
                    pass

            # Close loop if fetchmail exited without timeout (e.g. without IDLE or with POP3) and log exit
            break

    finally:
        print('{} Exited fetchmail {}'.format(time.strftime("%b %d %H:%M:%S"), user_info))
        if debug and fetchmail_output:
            try:
                print(fetchmail_output.decode('utf8', errors='ignore'))
            except Exception:
                pass
        # Clean up pidfile if present
        try:
            pidfile = os.path.join(fetchmailhome, 'fetchmail.pid')
            if os.path.exists(pidfile):
                os.remove(pidfile)
        except Exception:
            pass
        sys.stdout.flush()


# --- Controller implementation using builtin http.server ---

class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "mailu-fetchmail-webhook/1.0"

    def _set_response(self, code=200, content_type='application/json'):
        self.send_response(code)
        self.send_header('Content-type', content_type)
        self.end_headers()

    def do_POST(self):
        # Only support reload endpoint
        if self.path != '/internal/fetch/reload':
            self._set_response(404)
            self.wfile.write(b'{}')
            return

        # Simple shared secret header
        secret = os.environ.get('FETCHMAIL_WEBHOOK_SECRET', None)
        if secret:
            header = self.headers.get('X-FETCHMAIL-SECRET')
            if header != secret:
                self._set_response(401)
                self.wfile.write(b'{"error":"unauthorized"}')
                return

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b''
        try:
            data = json.loads(body.decode('utf8')) if body else {}
        except Exception:
            self._set_response(400)
            self.wfile.write(b'{"error":"invalid json"}')
            return

        ids = []
        if isinstance(data, dict) and 'id' in data:
            ids = [data['id']]
        elif isinstance(data, list):
            ids = data
        else:
            # allow empty body to trigger full reconcile
            ids = []

        # enqueue the ids for controller processing
        for i in ids:
            try:
                self.server.controller_queue.put_nowait(i)
            except queue.Full:
                pass

        # if no id provided, enqueue a full reconcile marker (None)
        if not ids:
            try:
                self.server.controller_queue.put_nowait(None)
            except queue.Full:
                pass

        self._set_response(200)
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        # Avoid noisy logs; print with timestamp instead
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format%args))


class ThreadedHTTPServer(HTTPServer):
    # attach a controller_queue attribute dynamically
    pass


class Controller:
    def __init__(self, config, debug=False, bind_address='0.0.0.0', port=8888):
        self.config = config
        self.debug = debug
        self.bind_address = bind_address
        self.port = int(os.environ.get('FETCHMAIL_WEBHOOK_PORT', port))
        self.admin_addr = os.environ.get('ADMIN_ADDRESS')
        self.workers = {}  # fetch_id -> {'proc': Process, 'stop': Event, 'hash': str, 'fetchmailhome': str}
        self.queue = queue.Queue(maxsize=100)
        self.httpd = None
        self.server_thread = None
        self.running = False
        self.lock = threading.Lock()

    def compute_hash(self, fetch):
        try:
            h = hashlib.sha256(json.dumps(fetch, sort_keys=True).encode('utf8')).hexdigest()
            return h
        except Exception:
            return str(hash(repr(fetch)))

    def start_http_server(self):
        handler = WebhookHandler
        # attach controller queue to server via handler.server
        self.httpd = ThreadedHTTPServer((self.bind_address, self.port), handler)
        # expose queue to handler via server
        self.httpd.controller_queue = self.queue

        def serve():
            try:
                print(f"Starting webhook server on {self.bind_address}:{self.port}")
                self.httpd.serve_forever()
            except Exception as e:
                print("Webhook server stopped:", e)

        self.server_thread = threading.Thread(target=serve, daemon=True)
        self.server_thread.start()

    def stop_http_server(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass

    def reconcile_all(self):
        try:
            fetches = requests.get(f"http://{self.admin_addr}:8080/internal/fetch", timeout=5).json()
        except Exception:
            if self.debug:
                print("Failed to fetch fetchlist from admin for reconcile_all")
            fetches = []

        ids = set()
        for fetch in fetches:
            ids.add(fetch['id'])
            self._ensure_worker(fetch)

        # stop workers that are no longer present
        with self.lock:
            for fid in list(self.workers.keys()):
                if fid not in ids:
                    self._stop_worker(fid)

    def reconcile_id(self, fid):
        try:
            fetches = requests.get(f"http://{self.admin_addr}:8080/internal/fetch", timeout=5).json()
        except Exception:
            if self.debug:
                print(f"Failed to fetch fetch {fid} from admin for reconcile_id")
            fetches = []

        find = None
        for fetch in fetches:
            if fetch['id'] == fid:
                find = fetch
                break

        if find:
            self._ensure_worker(find)
        else:
            # fetch removed or disabled: stop worker if running
            with self.lock:
                if fid in self.workers:
                    self._stop_worker(fid)

    def _ensure_worker(self, fetch):
        fid = fetch['id']
        h = self.compute_hash(fetch)
        with self.lock:
            rec = self.workers.get(fid)
            if rec:
                if rec['hash'] == h:
                    # no-op
                    return
                # config changed: restart
                self._stop_worker(fid)

            # start new worker
            fetch_instance_name = "%s" % (fetch["username"] + "_" + fetch["host"]) 
            fetchmailhome = "/data/" + re.sub(r'[^a-zA-Z0-9\\s]', '', fetch_instance_name)
            stop_event = Event()
            p = Process(target=worker, args=(self.debug, fetch, fetchmailhome, stop_event,))
            p.start()
            self.workers[fid] = {
                'proc': p,
                'stop': stop_event,
                'hash': h,
                'fetchmailhome': fetchmailhome,
                'started': datetime.now()
            }
            if self.debug:
                print(f"Started worker for fetch id {fid} ({fetch['username']}@{fetch['host']})")

    def _stop_worker(self, fid, timeout=10):
        rec = self.workers.get(fid)
        if not rec:
            return
        p = rec['proc']
        stop_event = rec['stop']
        # Ask worker to stop
        stop_event.set()
        # Wait for process to exit
        p.join(timeout)
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
            p.join(5)
            if p.is_alive():
                try:
                    p.kill()
                except Exception:
                    pass
        # Attempt to clean up pidfile
        try:
            pidfile = os.path.join(rec['fetchmailhome'], 'fetchmail.pid')
            if os.path.exists(pidfile):
                os.remove(pidfile)
        except Exception:
            pass

        with self.lock:
            try:
                del self.workers[fid]
            except KeyError:
                pass

        if self.debug:
            print(f"Stopped worker for fetch id {fid}")

    def start(self):
        self.running = True
        # start HTTP server
        self.start_http_server()
        # initial reconcile
        self.reconcile_all()

        # Start queue processor thread
        def process_queue():
            while self.running:
                try:
                    item = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                if item is None:
                    # full reconcile
                    try:
                        self.reconcile_all()
                    except Exception:
                        pass
                else:
                    try:
                        self.reconcile_id(item)
                    except Exception:
                        pass

        t = threading.Thread(target=process_queue, daemon=True)
        t.start()

    def stop(self):
        self.running = False
        self.stop_http_server()
        # stop all workers
        with self.lock:
            for fid in list(self.workers.keys()):
                self._stop_worker(fid)


def run(debug):
    try:
        # Start controller with webhook and fallback reconcile
        controller = Controller(system.set_env(), debug=debug, bind_address=os.environ.get('FETCHMAIL_WEBHOOK_BIND','0.0.0.0'), port=int(os.environ.get('FETCHMAIL_WEBHOOK_PORT', 8888)))
        controller.start()

        delay = int(os.environ.get('FETCHMAIL_DELAY', 60))
        while True:
            try:
                # periodic reconciliation as a fallback
                controller.reconcile_all()
            except Exception:
                traceback.print_exc()
            time.sleep(delay)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    config = system.set_env()
    # Remove any stale lockfiles in /data
    print("{} Starting mailu-fetchmail version {}. Cleaning up...".format(time.strftime("%b %d %H:%M:%S"), VERSION))
    id_fetchmail = getpwnam('fetchmail')
    try:
        os.chown("/data/", id_fetchmail.pw_uid, id_fetchmail.pw_gid)
    except Exception:
        pass
    lockfiles = Path("/data")
    for item in lockfiles.rglob("fetchmail.pid"):
        try:
            os.remove(item)
        except Exception:
            pass
    # Give other containers some time to start before starting fetchmail
    time.sleep(20)

    # Start controller/main loop
    run(config.get('DEBUG', False))
