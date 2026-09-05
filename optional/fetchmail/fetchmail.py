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
from multiprocessing import Process
import re

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


def fetchmail(fetchmailrc, fetchmailhome):
    with tempfile.NamedTemporaryFile() as handler:
        # Prepare fetchmailrc file
        handler.write(fetchmailrc.encode("utf8"))
        handler.flush()

        # Prepare command
        fetchmail_custom_options = os.environ.get("FETCHMAIL_OPTIONS", "")
        command = FETCHMAIL.format(fetchmailhome, fetchmailhome, fetchmail_custom_options, shlex.quote(handler.name))

        # Ensure correct $FETCHMAILHOME for secondary environment and start command
        command_env = os.environ.copy()
        command_env["FETCHMAILHOME"] = fetchmailhome
        output = subprocess.run(command, check=True, capture_output=True, shell=True, env=command_env)
        return output


def worker(debug, fetch, fetchmailhome):

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
    options += " ssl" if fetch["tls"] else " sslproto \'\'"
    options += " keep" if fetch["keep"] else " fetchall"

    # Build fetchmailrc
    folders = f"folders {",".join(f'"{imaputf7encode(item).replace('"',r"\34")}"' for item in fetch["folders"]) or '"INBOX"'}"
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
    while retry_counter <= 1024:
        print('{} Starting fetchmail {}'.format(time.strftime("%b %d %H:%M:%S"), user_info))
        sys.stdout.flush()
        fetchmail_output = ""
        try:
            fetchmail_output = fetchmail(fetchmailrc, fetchmailhome).stdout
            error_message = ""
        except subprocess.CalledProcessError as error:
            error_message = error.output.decode("utf8")

            # IMAP IDLE Timeout is not an error, but requires fetchmail to restart (thus continue in loop)
            if error.returncode == 2:
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
            if not error_message.startswith("fetchmail: No mail"):
                print('{} {}'.format(error_message.rstrip(),user_info))
                sys.stdout.flush()

        finally:
            #Log iteration in database including error message if any
            requests.post("http://{}:8080/internal/fetch/{}".format(os.environ['ADMIN_ADDRESS'],fetch['id']),
                json=error_message.split('\n')[0])

        # Close loop if fetchmail exited without timeout (e.g. without IDLE or with POP3) and log exit
        break
    print('{} Exited fetchmail {}'.format(time.strftime("%b %d %H:%M:%S"), user_info))
    if debug:
        print(fetchmail_output)
    sys.stdout.flush()


def run(debug):
    try:
        fetches = requests.get(f"http://{os.environ['ADMIN_ADDRESS']}:8080/internal/fetch").json()
        processes = []
        for fetch in fetches:
            # Defining instance name with username and host to trigger an error if same mailbox is fetched for multiple users
            fetch_instance_name = "%s" % (fetch["username"] + "_" + fetch["host"])
            fetchmailhome = "/data/" + re.sub(r'[^a-zA-Z0-9\s]', '', fetch_instance_name)
            # Start worker for fetch if no other worker is already running on this (can recover/restart failed workers in idle mode or avoids conflicts if FETCHMAIL_DELAY is too short and previous non-idle process still runs)
            if not os.path.exists(fetchmailhome + "/fetchmail.pid"):
                p = Process(target=worker, args=(debug,fetch,fetchmailhome,))
                p.start()
                processes.append(p)
        # Clean up finished child processes
        for p in processes:
            if not p.is_alive():
                p.join()

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    config = system.set_env()
    # Remove any stale lockfiles in /data
    print("{} Starting mailu-fetchmail version {}. Cleaning up...".format(time.strftime("%b %d %H:%M:%S"), VERSION))
    id_fetchmail = getpwnam('fetchmail')
    os.chown("/data/", id_fetchmail.pw_uid, id_fetchmail.pw_gid)
    lockfiles = Path("/data")
    for item in lockfiles.rglob("fetchmail.pid"):
        os.remove(item)
    # Give other containers some time to start before starting fetchmail
    time.sleep(20)
    delay = int(os.environ.get('FETCHMAIL_DELAY', 60))

    while True:

        if not config.get('FETCHMAIL_ENABLED', True):
            print("Fetchmail disabled, skipping...")
            time.sleep(delay)
            continue

        run(config.get('DEBUG', False))
        sys.stdout.flush()

        print("{} Sleeping for {} seconds".format(time.strftime("%b %d %H:%M:%S"), delay))
        time.sleep(delay)
