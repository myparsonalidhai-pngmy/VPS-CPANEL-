import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sulav-vps-secret-2025")

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

RUNNING_PROCESSES = {}


# ─── Data helpers ─────────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {}
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


# ─── Decorators ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─── Process helpers ──────────────────────────────────────────────────────────

def is_process_alive(pid):
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    else:
        return ["python", "-u", main_file]

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in data["servers"].items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
    if changed:
        save_data(data)

_sync_process_status()


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username:
            return render_template("login.html", error="Enter a username")
        data = load_data()
        user = data["users"].get(username)
        if user:
            stored_hash = user.get("password_hash", "")
            if stored_hash and stored_hash != hash_password(password):
                return render_template("login.html", error="Wrong password")
            elif not stored_hash and password:
                data["users"][username]["password_hash"] = hash_password(password)
                save_data(data)
        else:
            data["users"][username] = {
                "joined": datetime.now().isoformat(),
                "password_hash": hash_password(password) if password else ""
            }
            save_data(data)
        session["username"] = username
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    changed = False
    for name, cfg in user_servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            changed = True
    if changed:
        save_data(data)
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=username)


# ─── System stats API ─────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})


# ─── Server management ────────────────────────────────────────────────────────

@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "",
        "port": 8080,
        "packages": [],
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and cfg.get("owner") == session["username"]:
        pid = cfg.get("pid")
        if pid:
            kill_process(pid)
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    pid = cfg.get("pid")
    if pid and not is_process_alive(pid):
        cfg["status"] = "stopped"
        cfg["pid"] = None
        data["servers"][name] = cfg
        save_data(data)
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template("server.html", server_name=name, config=cfg, files=files)

def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result


# ─── Upload ───────────────────────────────────────────────────────────────────

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file"})
    f = request.files["file"]
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    extracted_files = []
    if f.filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                z.extractall(extract_dir)
                extracted_files = [m.filename for m in z.infolist() if not m.is_dir()]
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        dest = extract_dir / f.filename
        shutil.copy(upload_path, dest)
        upload_path.unlink(missing_ok=True)
        extracted_files = [f.filename]
        if not cfg.get("main_file") and f.filename.endswith((".py", ".js", ".ts")):
            cfg["main_file"] = f.filename
            data["servers"][name] = cfg
            save_data(data)
    return jsonify({"success": True, "files": extracted_files})


# ─── Packages ─────────────────────────────────────────────────────────────────

@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
def install_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    pkg_name = payload.get("name", "").strip()
    pkg_ver = payload.get("version", "").strip()
    if not pkg_name:
        return jsonify({"success": False, "error": "Package name required"})
    install_str = f"{pkg_name}=={pkg_ver}" if pkg_ver else pkg_name
    try:
        result = subprocess.run(["pip", "install", install_str], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return jsonify({"success": False, "error": result.stderr[:400] or result.stdout[:400]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    pkgs = cfg.get("packages", [])
    pkgs = [p for p in pkgs if p["name"] != pkg_name]
    pkgs.append({"name": pkg_name, "version": pkg_ver or "", "installed_at": datetime.now().isoformat()})
    cfg["packages"] = pkgs
    data["servers"][name] = cfg
    save_data(data)
    req_path = SERVERS_DIR / name / "extracted" / "requirements.txt"
    try:
        lines = req_path.read_text().splitlines() if req_path.exists() else []
        lines = [l for l in lines if not l.lower().startswith(pkg_name.lower())]
        lines.append(install_str)
        req_path.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
    return jsonify({"success": True, "package": pkg_name})

@app.route("/server/<name>/packages/remove", methods=["POST"])
@login_required
def remove_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    payload = request.get_json()
    pkg_name = payload.get("name", "")
    cfg["packages"] = [p for p in cfg.get("packages", []) if p["name"] != pkg_name]
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})


# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})


# ─── Start / Stop ─────────────────────────────────────────────────────────────

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    pid = cfg.get("pid")
    if pid and is_process_alive(pid):
        return jsonify({"success": False, "error": "Already running"})
    main_file = cfg.get("main_file") or "main.py"
    extract_dir = SERVERS_DIR / name / "extracted"
    main_path = extract_dir / main_file
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = get_run_command(cfg.get("runtime", "python"), main_file)
    env = os.environ.copy()
    env["PORT"] = str(cfg.get("port", 8080))
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    pid = cfg.get("pid")
    stopped = False
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            entry["log_file"].close()
        except Exception:
            pass
        del RUNNING_PROCESSES[name]
        stopped = True
    if pid and not stopped:
        kill_process(pid)
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    except Exception:
        pass
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})


# ─── Logs ─────────────────────────────────────────────────────────────────────

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet. Start the server to see output."})
    try:
        content = log_path.read_text(errors="replace")
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (showing last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        log_path.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
