import os
import re
import json
import shutil
import socket
import subprocess
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])
LOG_PATH = r".\QRemote.log"

HELP_TEXT = (
    "QRemote Bot\n\n"
    "/shutdown — shut down this PC in 60 s\n"
    "/reboot   — reboot this PC in 60 s\n"
    "/cancel   — abort a pending shutdown or reboot\n"
    "/status   — confirm the bot is online\n"
    "/marco    — say hello\n"
    "/tasks    — list all runnable Remote tasks\n"
    "/help     — show this message\n\n"
    "Any /command whose name matches a 'Remote<Command>' Task Scheduler task will run it.\n"
    "Example: /winver runs RemoteWinver"
)


class _JsonHandler(logging.FileHandler):
    def emit(self, record):
        now = datetime.now().astimezone()
        # Pacific-specific: astimezone() gives a fixed-offset tzinfo with no
        # .dst() info, so derive PDT/PST from the UTC offset itself.
        tz_abbr = "PDT" if now.utcoffset() == timedelta(hours=-7) else "PST"
        entry = {
            "timestamp": now.strftime(f"%y-%m-%d %I:%M:%S%p {tz_abbr}"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        self.stream.write(json.dumps(entry) + "\n")
        self.flush()


logger = logging.getLogger("qremote")
logger.setLevel(logging.INFO)
logger.addHandler(_JsonHandler(LOG_PATH))
logger.addHandler(logging.StreamHandler())


def send_message(chat_id: int, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error(f"sendMessage failed: {exc}")


def run_cmd(args: list) -> bool:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.returncode == 0

def list_remote_tasks() -> list[str]:
    result = subprocess.run(
        ["schtasks", "/query", "/fo", "CSV", "/nh"],
        capture_output=True, text=True,
    )
    tasks = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # CSV row: "TaskName","NextRunTime","Status"
        # schtasks prefixes root-level task names with "\" — strip it before comparing
        name = line.strip().strip('"').split('","')[0].lstrip("\\")
        if name.startswith("Remote"):
            tasks.append(name)
    return sorted(tasks)


_CF_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.log")


def start_tunnel() -> subprocess.Popen | None:
    """Spawn a cloudflared quick tunnel, capture its URL, and register the Telegram webhook."""
    if not shutil.which("cloudflared"):
        logger.warning("cloudflared not found in PATH — skipping auto-tunnel")
        return None

    # Write to a file instead of a pipe — avoids Windows pipe-buffering issues
    with open(_CF_LOG, "w") as lf:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://localhost:5000"],
            stdout=lf,
            stderr=lf,
        )

    # Poll the log file until the URL appears (up to 30 s)
    url = None
    for _ in range(30):
        time.sleep(1)
        try:
            content = open(_CF_LOG, encoding="utf-8", errors="ignore").read()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            if m:
                url = m.group(0)
                break
        except OSError:
            pass

    if not url:
        logger.error("cloudflared started but tunnel URL not found after 30 s — terminating")
        proc.terminate()
        return None

    logger.info(f"Tunnel URL captured: {url}")
    hostname = url.removeprefix("https://")

    # Wait until local DNS resolves the hostname (up to 10 s) before telling Telegram
    for i in range(10):
        try:
            socket.getaddrinfo(hostname, 443)
            logger.info(f"DNS resolved after {i} s")
            break
        except socket.gaierror:
            logger.info(f"Waiting for DNS to resolve... ({i + 1}/10 s)")
            time.sleep(1)
    else:
        logger.warning("DNS not resolved locally after 10 s — attempting setWebhook anyway")

    webhook_url = f"{url}/webhook"
    for attempt in range(1, 21):
        try:
            time.sleep(5)
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": webhook_url},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"Webhook registered: {webhook_url}")
                send_message(ALLOWED_USER_ID, f"Bot online. Tunnel: {url}")
                break
            else:
                logger.warning(f"setWebhook attempt {attempt}/20 failed: {data}")
        except requests.RequestException as exc:
            logger.warning(f"setWebhook attempt {attempt}/20 error: {exc}")
    else:
        logger.error("setWebhook failed after 20 attempts — webhook not registered")

    return proc


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        abort(400)

    message = data.get("message", {})
    user_id = message.get("from", {}).get("id")
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if user_id != ALLOWED_USER_ID:
        logger.warning(f"Rejected request from user_id={user_id}")
        return "OK", 200

    logger.info(f"Command '{text}' from user_id={user_id}")

    if text == "/shutdown":
        ok = run_cmd(["shutdown", "/s", "/t", "60"])
        reply = "Shutdown in 60 s — use /cancel to abort." if ok else "ERROR: shutdown command failed."
    elif text == "/reboot":
        ok = run_cmd(["shutdown", "/r", "/t", "60"])
        reply = "Reboot in 60 s — use /cancel to abort." if ok else "ERROR: reboot command failed."
    elif text == "/cancel":
        result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
        reply = "Cancelled." if result.returncode == 0 else "No pending shutdown to cancel."
    elif text == "/marco":
        reply = "Polo!"
    elif text == "/status":
        reply = "Online."
    elif text == "/tasks":
        tasks = list_remote_tasks()
        if tasks:
            lines = [f"  /{t[len('Remote'):].lower()} → {t}" for t in tasks]
            reply = "Available Remote tasks:\n" + "\n".join(lines)
        else:
            reply = "No Remote tasks found in Task Scheduler."
    elif text in ("/start", "/help"):
        reply = HELP_TEXT
    elif text.startswith("/"):
        cmd = text[1:].split()[0]
        task_name = "Remote" + cmd.capitalize()
        if task_name in list_remote_tasks():
            ok = run_cmd(["schtasks", "/run", "/tn", task_name])
            reply = f"Running task: {task_name}" if ok else f"ERROR: failed to run {task_name}."
        else:
            reply = f"Unknown command.\n\n{HELP_TEXT}"
    else:
        reply = f"Unknown command.\n\n{HELP_TEXT}"

    send_message(chat_id, reply)
    return "OK", 200

@app.route("/health", methods=["GET"])
def health_check():
    return {"status": "running"}, 200

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    if ALLOWED_USER_ID == 0:
        raise SystemExit("TELEGRAM_USER_ID is not set in .env")
    logger.info("Starting QRemote Bot on port 5000")
    start_tunnel()
    app.run(host="0.0.0.0", port=5000)
