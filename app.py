import os
import json
import select
import subprocess
import threading
import time
import uuid
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import chat_store

app = Flask(__name__)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")
ADMIN_USER = "shamless"

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return set(json.load(f))
    return {ADMIN_USER}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(sorted(list(users)), f)

chat_store.init_db()

API_KEYS_FILE = os.path.join(os.path.dirname(__file__), "api_keys.json")
AGENTS_FILE = os.path.join(os.path.dirname(__file__), "agents.json")

def load_agents():
    if os.path.exists(AGENTS_FILE):
        with open(AGENTS_FILE) as f:
            return json.load(f)
    return []

def save_agents(agents):
    with open(AGENTS_FILE, 'w') as f:
        json.dump(agents, f, ensure_ascii=False, indent=2)

def load_api_keys():
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE) as f:
            return json.load(f)
    return {}

def save_api_keys(keys):
    with open(API_KEYS_FILE, 'w') as f:
        json.dump(keys, f)

MODEL_CONFIG = {
    "claude-cli": {"type": "cli"},
    "claude-api": {
        "type": "api",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "key_name": "claude_api_key",
    },
    "deepseek": {
        "type": "api",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "key_name": "deepseek_api_key",
    },
}

def stream_claude_api(full_prompt, history, api_key):
    """Stream response from Claude API (Anthropic native format)."""
    messages = []
    for msg in history[:-1]:
        messages.append({"role": msg["role"], "content": msg["content"][:2000]})
    messages.append({"role": "user", "content": full_prompt})

    body = json.dumps({
        "model": MODEL_CONFIG["claude-api"]["model"],
        "max_tokens": 4096,
        "stream": True,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        MODEL_CONFIG["claude-api"]["url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    resp = urllib.request.urlopen(req, timeout=300)
    buffer = b""
    for chunk in iter(lambda: resp.read(1024), b""):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.decode().strip()
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                    if data.get("type") == "content_block_delta":
                        text = data.get("delta", {}).get("text", "")
                        if text:
                            yield text
                except json.JSONDecodeError:
                    pass

def stream_openai_compatible(full_prompt, history, api_key, model_id):
    """Stream response from OpenAI-compatible API (DeepSeek, etc.)."""
    config = MODEL_CONFIG[model_id]
    messages = []
    for msg in history[:-1]:
        messages.append({"role": msg["role"], "content": msg["content"][:2000]})
    messages.append({"role": "user", "content": full_prompt})

    body = json.dumps({
        "model": config["model"],
        "stream": True,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        config["url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    resp = urllib.request.urlopen(req, timeout=300)
    buffer = b""
    for chunk in iter(lambda: resp.read(1024), b""):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.decode().strip()
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        yield text
                except json.JSONDecodeError:
                    pass

def get_user():
    token = request.headers.get("X-Auth-Token", "")
    if token in load_users():
        return token
    return None

def is_admin():
    return get_user() == ADMIN_USER

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.json or {}
    token = data.get("token", "")
    users = load_users()
    if token in users:
        if token != ADMIN_USER:
            chat_store.clear_user_conversations(token)
        return jsonify({"ok": True, "user": token, "is_admin": token == ADMIN_USER})
    return jsonify({"ok": False, "error": "Invalid token"}), 401

@app.route("/api/chat", methods=["POST"])
def chat():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    session_name = data.get("session_id", "")
    message = data.get("message", "")
    model_id = data.get("model", "claude-cli")

    chat_store.add_message(session_name, "user", message, user=user)

    MAX_CONTEXT_CHARS = 12000
    MAX_MSG_CHARS = 2000

    history = chat_store.get_messages(session_name)

    def truncate_msg(text, limit=MAX_MSG_CHARS):
        if len(text) <= limit:
            return text
        return text[:limit] + "...[truncated]"

    if len(history) <= 1:
        full_prompt = message
    else:
        context_parts = []
        total_chars = 0
        for msg in reversed(history[:-1]):
            content = truncate_msg(msg["content"])
            prefix = "User" if msg["role"] == "user" else "Assistant"
            part = f"{prefix}: {content}"
            if total_chars + len(part) > MAX_CONTEXT_CHARS:
                break
            context_parts.append(part)
            total_chars += len(part)

        context_parts.reverse()
        if context_parts:
            if model_id == "claude-cli":
                full_prompt = "Previous conversation:\n" + "\n".join(context_parts) + "\n\nNow respond to the latest message: " + message
            else:
                full_prompt = message
        else:
            full_prompt = message

    def generate_cli():
        """Generate via Claude CLI (original method)."""
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            cli_prompt = full_prompt
            if model_id == "claude-cli":
                cli_prompt = full_prompt
            proc = subprocess.Popen(
                ["claude", "-p", cli_prompt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1
            )

            full_response = ""
            timeout_seconds = 600
            start_time = time.time()
            last_heartbeat = time.time()
            heartbeat_interval = 15
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout_seconds:
                    proc.kill()
                    if full_response:
                        full_response += "\n\n[Response timed out after 10 minutes]"
                    else:
                        full_response = "[Response timed out after 10 minutes]"
                    timeout_text = '\n\n[超时：回复超过10分钟已中断]'
                    yield f"data: {json.dumps({'text': timeout_text})}\n\n"
                    break

                ready, _, _ = select.select([proc.stdout], [], [], 2.0)
                if ready:
                    chunk = proc.stdout.read(20)
                    if not chunk:
                        break
                    full_response += chunk
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    last_heartbeat = time.time()
                elif proc.poll() is not None:
                    remaining = proc.stdout.read()
                    if remaining:
                        full_response += remaining
                        yield f"data: {json.dumps({'text': remaining})}\n\n"
                    break
                else:
                    if time.time() - last_heartbeat >= heartbeat_interval:
                        yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                        last_heartbeat = time.time()

            proc.wait()
            if proc.returncode != 0:
                err = proc.stderr.read()
                if err and not full_response:
                    yield f"data: {json.dumps({'error': err})}\n\n"

            if full_response:
                chat_store.add_message(session_name, "assistant", full_response, user=user)

            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    def generate_api():
        """Generate via Claude API or DeepSeek API."""
        try:
            config = MODEL_CONFIG.get(model_id)
            if not config:
                yield f"data: {json.dumps({'error': f'Unknown model: {model_id}'})}\n\n"
                return

            api_keys = load_api_keys()
            api_key = api_keys.get(config["key_name"], "")
            if not api_key:
                yield f"data: {json.dumps({'error': f'API key not set for {model_id}. Please configure it in Settings.'})}\n\n"
                return

            full_response = ""
            if model_id == "claude-api":
                streamer = stream_claude_api(full_prompt, history, api_key)
            else:
                streamer = stream_openai_compatible(full_prompt, history, api_key, model_id)

            for text in streamer:
                full_response += text
                yield f"data: {json.dumps({'text': text})}\n\n"

            if full_response:
                chat_store.add_message(session_name, "assistant", full_response, user=user)

            yield f"data: {json.dumps({'done': True})}\n\n"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else str(e)
            yield f"data: {json.dumps({'error': f'API Error {e.code}: {err_body[:200]}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    if model_id == "claude-cli":
        gen = generate_cli()
    else:
        gen = generate_api()

    return Response(stream_with_context(gen), mimetype="text/event-stream")

@app.route("/api/clear", methods=["POST"])
def clear():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    session_name = data.get("session_id", "")
    chat_store.delete_conversation(session_name)
    return jsonify({"ok": True})

@app.route("/api/sessions", methods=["GET"])
def sessions():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    convs = chat_store.list_conversations(user=user)
    # Return in the same format frontend expects
    result = []
    for c in convs:
        result.append({
            "id": c["name"],
            "title": c["name"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
        })
    return jsonify(result)

@app.route("/api/sessions/<path:session_id>/messages", methods=["GET"])
def session_messages(session_id):
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(chat_store.get_messages(session_id))

@app.route("/api/sessions/<path:session_id>", methods=["DELETE"])
def delete_session(session_id):
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    chat_store.delete_conversation(session_id)
    return jsonify({"ok": True})

@app.route("/api/sessions/<path:session_id>/rename", methods=["PUT"])
def rename_session(session_id):
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    chat_store.rename_conversation(session_id, title)
    return jsonify({"ok": True, "new_id": title})

@app.route("/api/models", methods=["GET"])
def list_models():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    api_keys = load_api_keys()
    models = []
    for mid, conf in MODEL_CONFIG.items():
        m = {"id": mid, "name": mid}
        if mid == "claude-cli":
            m["name"] = "Claude CLI"
            m["available"] = True
        elif mid == "claude-api":
            m["name"] = "Claude API"
            m["available"] = bool(api_keys.get(conf["key_name"]))
        elif mid == "deepseek":
            m["name"] = "DeepSeek"
            m["available"] = bool(api_keys.get(conf["key_name"]))
        models.append(m)
    return jsonify(models)

@app.route("/api/api-keys", methods=["GET"])
def get_api_keys():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    keys = load_api_keys()
    masked = {}
    for k, v in keys.items():
        if v:
            masked[k] = v[:8] + "..." + v[-4:]
        else:
            masked[k] = ""
    return jsonify(masked)

@app.route("/api/api-keys", methods=["POST"])
def set_api_keys():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    keys = load_api_keys()
    for k in ["claude_api_key", "deepseek_api_key"]:
        if k in data and data[k]:
            keys[k] = data[k].strip()
    save_api_keys(keys)
    return jsonify({"ok": True})


def describe_cron(raw, task_type="cron", unit="", source=""):
    text = raw.lower()
    cmd = raw

    if task_type in ("cron", "cron.d"):
        parts = raw.split()
        if len(parts) > 5:
            cmd = " ".join(parts[5:])
        if task_type == "cron.d" and len(parts) > 6:
            cmd = " ".join(parts[6:])
        text = cmd.lower()

    if "certbot" in text or "letsencrypt" in text or "ssl" in text:
        return "自动续期SSL证书，保持HTTPS安全连接"
    if "backup" in text or "rsync" in text or "mysqldump" in text or "pg_dump" in text:
        return "定时备份数据，防止数据丢失"
    if "logrotate" in text or "log" in text and "rotate" in text:
        return "日志轮转清理，防止磁盘空间不足"
    if "apt" in text and ("update" in text or "upgrade" in text):
        return "自动更新系统软件包"
    if "python" in text or ".py" in text:
        script = ""
        for p in cmd.split():
            if p.endswith(".py"):
                script = os.path.basename(p)
                break
        if script:
            return f"定时运行Python脚本 {script}"
        return "定时运行Python脚本任务"
    if "node" in text or ".js" in text:
        return "定时运行Node.js脚本任务"
    if "curl" in text or "wget" in text:
        return "定时发送HTTP请求或下载文件"
    if "rm " in text or "find" in text and "delete" in text:
        return "定时清理临时文件或过期数据"
    if "mail" in text or "sendmail" in text:
        return "定时发送邮件通知"
    if "docker" in text:
        return "Docker容器相关定时任务"
    if "nginx" in text:
        return "Nginx服务相关定时操作"
    if "reboot" in text or "shutdown" in text:
        return "定时重启或关机任务"
    if "monitor" in text or "check" in text or "health" in text:
        return "定时健康检查或监控任务"
    if "sync" in text:
        return "定时数据同步任务"
    if "cron" in text and "clean" in text:
        return "清理过期的定时任务会话"

    if task_type == "systemd":
        name = unit.replace(".timer", "").lower()
        if "apt" in name:
            return "APT软件包自动更新检查"
        if "fstrim" in name:
            return "SSD磁盘TRIM优化，延长硬盘寿命"
        if "logrotate" in name:
            return "系统日志自动轮转清理"
        if "man-db" in name:
            return "更新man手册页数据库"
        if "systemd-tmpfiles" in name:
            return "清理系统临时文件目录"
        if "motd" in name:
            return "更新登录欢迎信息"
        if "dpkg" in name:
            return "dpkg数据库维护"
        if "update-notifier" in name:
            return "检查可用的系统更新"
        if "snapd" in name:
            return "Snap应用自动更新管理"
        return f"系统定时服务：{unit.replace('.timer', '')}"

    if task_type == "cron.d" and source:
        return f"来自 {source} 的系统定时任务"

    return "服务器定时执行的计划任务"


@app.route("/api/crontab", methods=["GET"])
def crontab():
    tasks = []

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    desc = describe_cron(line, "cron")
                    tasks.append({"type": "cron", "raw": line, "desc": desc})
    except Exception:
        pass

    try:
        for fname in os.listdir("/etc/cron.d"):
            fpath = os.path.join("/etc/cron.d", fname)
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith("SHELL") and not line.startswith("PATH") and not line.startswith("MAILTO"):
                            desc = describe_cron(line, "cron.d", source=fname)
                            tasks.append({"type": "cron.d", "source": fname, "raw": line, "desc": desc})
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 5 and not line.startswith(" "):
                    timer_name = ""
                    activates = ""
                    for p in parts:
                        if p.endswith(".timer"):
                            timer_name = p
                        elif p.endswith(".service"):
                            activates = p
                    if timer_name:
                        desc = describe_cron(line, "systemd", unit=timer_name)
                        tasks.append({
                            "type": "systemd",
                            "unit": timer_name,
                            "activates": activates,
                            "raw": line,
                            "desc": desc
                        })
    except Exception:
        pass

    return jsonify(tasks)


@app.route("/api/download/<path:filename>")
def download_file(filename):
    """Serve files from the project directory for download."""
    fpath = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.isfile(fpath):
        return "Not found", 404
    return send_file(fpath, as_attachment=True)


@app.route("/api/upload", methods=["POST"])
def upload():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": "Unsupported file type"}), 400
    fname = uuid.uuid4().hex + ext
    fpath = os.path.join(UPLOAD_DIR, fname)
    f.save(fpath)
    return jsonify({"ok": True, "url": f"/uploads/{fname}", "filename": f.filename})

@app.route("/uploads/<filename>")
def serve_upload(filename):
    fpath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(fpath):
        return "Not found", 404
    return send_file(fpath)

@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    users = sorted(list(load_users()))
    return jsonify(users)

@app.route("/api/admin/users", methods=["POST"])
def admin_add_user():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "Token cannot be empty"}), 400
    users = load_users()
    if token in users:
        return jsonify({"error": "User already exists"}), 400
    users.add(token)
    save_users(users)
    return jsonify({"ok": True})

@app.route("/api/admin/users/<token>", methods=["PUT"])
def admin_edit_user(token):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    if token == ADMIN_USER:
        return jsonify({"error": "Cannot modify admin user"}), 400
    data = request.json or {}
    new_token = data.get("token", "").strip()
    if not new_token:
        return jsonify({"error": "Token cannot be empty"}), 400
    users = load_users()
    if token not in users:
        return jsonify({"error": "User not found"}), 404
    if new_token != token and new_token in users:
        return jsonify({"error": "New token already exists"}), 400
    users.discard(token)
    users.add(new_token)
    save_users(users)
    chat_store.rename_user(token, new_token)
    return jsonify({"ok": True})

@app.route("/api/admin/users/<token>", methods=["DELETE"])
def admin_delete_user(token):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    if token == ADMIN_USER:
        return jsonify({"error": "Cannot delete admin user"}), 400
    users = load_users()
    if token not in users:
        return jsonify({"error": "User not found"}), 404
    users.discard(token)
    save_users(users)
    chat_store.clear_user_conversations(token)
    return jsonify({"ok": True})


# === Agent CRUD ===
@app.route("/api/agents", methods=["GET"])
def list_agents():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(load_agents())

@app.route("/api/agents", methods=["POST"])
def create_agent():
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    agents = load_agents()
    agent = {
        "id": uuid.uuid4().hex[:8],
        "name": data.get("name", "Agent").strip(),
        "model": data.get("model", "claude-cli"),
        "role": data.get("role", "").strip(),
        "system_prompt": data.get("system_prompt", "").strip(),
        "color": data.get("color", "#00aaff"),
    }
    agents.append(agent)
    save_agents(agents)
    return jsonify(agent)

@app.route("/api/agents/<agent_id>", methods=["PUT"])
def update_agent(agent_id):
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    agents = load_agents()
    for a in agents:
        if a["id"] == agent_id:
            if "name" in data: a["name"] = data["name"].strip()
            if "model" in data: a["model"] = data["model"]
            if "role" in data: a["role"] = data["role"].strip()
            if "system_prompt" in data: a["system_prompt"] = data["system_prompt"].strip()
            if "color" in data: a["color"] = data["color"]
            save_agents(agents)
            return jsonify(a)
    return jsonify({"error": "Agent not found"}), 404

@app.route("/api/agents/<agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    agents = load_agents()
    agents = [a for a in agents if a["id"] != agent_id]
    save_agents(agents)
    return jsonify({"ok": True})

@app.route("/api/agent-chat", methods=["POST"])
def agent_chat():
    """Chat with a specific agent - prepends system prompt."""
    user = get_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    session_name = data.get("session_id", "")
    message = data.get("message", "")
    agent_id = data.get("agent_id", "")

    agents = load_agents()
    agent = next((a for a in agents if a["id"] == agent_id), None)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    model_id = agent.get("model", "claude-cli")
    system_prompt = agent.get("system_prompt", "")

    # Save user message with agent tag
    chat_store.add_message(session_name, "user", message, user=user)

    MAX_CONTEXT_CHARS = 12000
    MAX_MSG_CHARS = 2000
    history = chat_store.get_messages(session_name)

    def truncate_msg(text, limit=MAX_MSG_CHARS):
        if len(text) <= limit:
            return text
        return text[:limit] + "...[truncated]"

    # Build prompt with system prompt
    if len(history) <= 1:
        if system_prompt:
            full_prompt = f"System instructions: {system_prompt}\n\nUser message: {message}"
        else:
            full_prompt = message
    else:
        context_parts = []
        total_chars = 0
        for msg in reversed(history[:-1]):
            content = truncate_msg(msg["content"])
            prefix = "User" if msg["role"] == "user" else "Assistant"
            part = f"{prefix}: {content}"
            if total_chars + len(part) > MAX_CONTEXT_CHARS:
                break
            context_parts.append(part)
            total_chars += len(part)
        context_parts.reverse()

        prompt_parts = []
        if system_prompt:
            prompt_parts.append(f"System instructions: {system_prompt}")
        if context_parts:
            prompt_parts.append("Previous conversation:\n" + "\n".join(context_parts))
        prompt_parts.append(f"Now respond to the latest message: {message}")
        full_prompt = "\n\n".join(prompt_parts)

    def generate_cli():
        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            proc = subprocess.Popen(
                ["claude", "-p", full_prompt],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, bufsize=1
            )
            full_response = ""
            timeout_seconds = 600
            start_time = time.time()
            last_heartbeat = time.time()
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout_seconds:
                    proc.kill()
                    yield f"data: {json.dumps({'text': '[超时中断]'})}\n\n"
                    break
                ready, _, _ = select.select([proc.stdout], [], [], 2.0)
                if ready:
                    chunk = proc.stdout.read(20)
                    if not chunk:
                        break
                    full_response += chunk
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    last_heartbeat = time.time()
                elif proc.poll() is not None:
                    remaining = proc.stdout.read()
                    if remaining:
                        full_response += remaining
                        yield f"data: {json.dumps({'text': remaining})}\n\n"
                    break
                else:
                    if time.time() - last_heartbeat >= 15:
                        yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                        last_heartbeat = time.time()
            proc.wait()
            if proc.returncode != 0:
                err = proc.stderr.read()
                if err and not full_response:
                    yield f"data: {json.dumps({'error': err})}\n\n"
            if full_response:
                tagged = f"[{agent['name']}] {full_response}"
                chat_store.add_message(session_name, "assistant", tagged, user=user)
            yield f"data: {json.dumps({'done': True, 'agent_name': agent['name'], 'agent_color': agent.get('color', '#00aaff')})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    def generate_api():
        try:
            config = MODEL_CONFIG.get(model_id)
            if not config:
                yield f"data: {json.dumps({'error': f'Unknown model: {model_id}'})}\n\n"
                return
            api_keys = load_api_keys()
            api_key = api_keys.get(config["key_name"], "")
            if not api_key:
                yield f"data: {json.dumps({'error': f'API key not set for {model_id}'})}\n\n"
                return
            full_response = ""
            if model_id == "claude-api":
                streamer = stream_claude_api(full_prompt, history, api_key)
            else:
                streamer = stream_openai_compatible(full_prompt, history, api_key, model_id)
            for text in streamer:
                full_response += text
                yield f"data: {json.dumps({'text': text})}\n\n"
            if full_response:
                tagged = f"[{agent['name']}] {full_response}"
                chat_store.add_message(session_name, "assistant", tagged, user=user)
            yield f"data: {json.dumps({'done': True, 'agent_name': agent['name'], 'agent_color': agent.get('color', '#00aaff')})}\n\n"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else str(e)
            yield f"data: {json.dumps({'error': f'API Error {e.code}: {err_body[:200]}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    if model_id == "claude-cli":
        gen = generate_cli()
    else:
        gen = generate_api()

    return Response(stream_with_context(gen), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7682, debug=False)
