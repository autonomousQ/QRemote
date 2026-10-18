import os
import re
import json
import ctypes
import secrets
import threading
import shutil
import socket
import subprocess
import time
import logging
from collections import deque
from datetime import datetime, timezone
from flask import Flask, request, abort, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])
LOG_PATH = r"C:\projects\QRemote\QRemote.log"

HELP_TEXT = (
    "QRemote Bot\n\n"
    "/shutdown — shut down this PC in 60 s\n"
    "/reboot   — reboot this PC in 60 s\n"
    "/cancel   — abort a pending shutdown or reboot\n"
    "/status   — confirm the bot is online + CPU/GPU readings\n"
    "/marco    — say hello\n"
    "/sensors  — CPU/GPU usage and temperature + chart link\n"
    "/tasks    — list all runnable Remote tasks\n"
    "/help     — show this message\n\n"
    "Any /command whose name matches a 'Remote<Command>' Task Scheduler task will run it.\n"
    "Example: /winver runs RemoteWinver"
)


class _JsonHandler(logging.FileHandler):
    def emit(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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


SAMPLE_SECS = 10
HISTORY = deque(maxlen=360)  # ponytail: 1 h in memory, lost on restart; persist to disk if longer history matters
STATS_KEY = secrets.token_urlsafe(16)  # new key each start; the link is only ever sent via Telegram
TUNNEL_URL = None
CPU_TEMP_FILE = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "QRemote", "cpu_temp.json")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _cpu_times() -> tuple[int, int]:
    idle, kern, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    return idle.value, kern.value + user.value  # kernel time includes idle


def _num(s: str):
    try:
        return round(float(s), 1)
    except ValueError:
        return None


def gpu_stats() -> tuple:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
        ).stdout.splitlines()[0]
        usage, temp = out.split(",")
        return _num(usage), _num(temp)
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return None, None


def cpu_temp():
    # Windows has no CPU temp API and the PawnIO driver is admin-only, so cpu_temp_helper.py
    # (SYSTEM task, see setup.ps1) writes the reading to a file; ignore it if the helper stopped.
    try:
        with open(CPU_TEMP_FILE) as f:
            data = json.load(f)
        return data["cpu_temp"] if time.time() - data["t"] < 3 * SAMPLE_SECS else None
    except (OSError, ValueError, KeyError):
        return None


def sampler() -> None:
    prev = _cpu_times()
    while True:
        time.sleep(SAMPLE_SECS)
        cur = _cpu_times()
        d_idle, d_total = cur[0] - prev[0], cur[1] - prev[1]
        prev = cur
        gpu, gpu_t = gpu_stats()
        HISTORY.append({
            "t": int(time.time()),
            "cpu": round(100 * (1 - d_idle / d_total), 1) if d_total else None,
            "cpu_temp": cpu_temp(),
            "gpu": gpu,
            "gpu_temp": gpu_t,
        })


def sensors_text() -> str:
    if not HISTORY:
        return f"No samples yet — try again in {SAMPLE_SECS} s."
    s = HISTORY[-1]
    f = lambda v, unit: "n/a" if v is None else f"{v:g} {unit}"
    lines = [
        f"CPU  {f(s['cpu'], '%')}  ·  {f(s['cpu_temp'], '°C')}",
        f"GPU  {f(s['gpu'], '%')}  ·  {f(s['gpu_temp'], '°C')}",
    ]
    if s["cpu_temp"] is None:
        lines.append("(CPU temp needs the QRemoteCpuTemp task from setup.ps1 running)")
    if TUNNEL_URL:
        lines.append(f"\nChart: {TUNNEL_URL}/stats?k={STATS_KEY}")
    return "\n".join(lines)


STATS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QRemote Sensors</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>:root{color-scheme:light dark}body{font:14px system-ui;margin:16px;background:Canvas;color:CanvasText}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.c{height:200px}</style>
</head><body><h3>QRemote — last hour (refreshes every 10 s)</h3><div class="g">
<div class="c"><canvas id="cpu"></canvas></div><div class="c"><canvas id="cpu_temp"></canvas></div>
<div class="c"><canvas id="gpu"></canvas></div><div class="c"><canvas id="gpu_temp"></canvas></div></div>
<script>
const S={cpu:["CPU usage %","#3b82f6",100],cpu_temp:["CPU temp °C","#ef4444"],gpu:["GPU usage %","#22c55e",100],gpu_temp:["GPU temp °C","#f59e0b"]};
const C={};for(const[k,[label,color,max]]of Object.entries(S))C[k]=new Chart(k,{type:"line",
 data:{labels:[],datasets:[{label,data:[],borderColor:color,backgroundColor:color+"33",fill:true,pointRadius:0,spanGaps:false}]},
 options:{animation:false,maintainAspectRatio:false,scales:{x:{ticks:{maxTicksLimit:6,maxRotation:0}},y:{min:0,max}}}});
async function tick(){const h=await (await fetch("stats.json"+location.search)).json();
 const labels=h.map(s=>new Date(s.t*1000).toLocaleTimeString());
 for(const k in C){C[k].data.labels=labels;C[k].data.datasets[0].data=h.map(s=>s[k]);C[k].update();}}
tick();setInterval(tick,10000);
</script></body></html>"""


def _check_key() -> None:
    if not secrets.compare_digest(request.args.get("k", ""), STATS_KEY):
        abort(404)


@app.route("/stats")
def stats_page():
    _check_key()
    return STATS_PAGE


@app.route("/stats.json")
def stats_json():
    _check_key()
    return jsonify(list(HISTORY))


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
    global TUNNEL_URL
    TUNNEL_URL = url
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
        reply = "Online.\n\n" + sensors_text()
    elif text == "/sensors":
        reply = sensors_text()
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
    threading.Thread(target=sampler, daemon=True).start()
    start_tunnel()
    app.run(host="0.0.0.0", port=5000)
