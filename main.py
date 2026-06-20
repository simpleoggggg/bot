# ============================================================
# main.py - NIROB PANEL v6.0 Enterprise
# Fixed: Live Terminal, Live Logs, ZIP Safety, File Integrity
# ============================================================

import eventlet
eventlet.monkey_patch()

import os
import json
import subprocess
import threading
import uuid
import shutil
import zipfile
import sys
import signal
import pty
import fcntl
import struct
import termios
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

# -----------------------------------------------------------
# App Setup
# -----------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nirob-v6-enterprise-secret-2026')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATA_FILE = 'data.json'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, 'projects')
os.makedirs(PROJECTS_DIR, exist_ok=True)

# Password hashing (simple but better than plaintext)
import hashlib

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hashed):
    return hash_password(password) == hashed

# -----------------------------------------------------------
# Data Layer
# -----------------------------------------------------------
def default_data():
    return {
        'users': {
            'xNIROB': {
                'password': hash_password('Hosting'),
                'role': 'admin',
                'created': str(datetime.now())
            }
        },
        'projects': {},
        'system_logs': [],
        'maintenance': False,
        'site_name': 'NIROB PANEL'
    }

def load_data():
    default = default_data()
    if os.path.exists(DATA_FILE):
        try:
            if os.path.getsize(DATA_FILE) > 0:
                with open(DATA_FILE, 'r') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        default.update(loaded)
                        return default
        except (json.JSONDecodeError, TypeError):
            pass
    return default

def save_data(data_to_save):
    with open(DATA_FILE, 'w') as f:
        json.dump(data_to_save, f, indent=2)

data = load_data()

# -----------------------------------------------------------
# Project Manager
# -----------------------------------------------------------
class ProjectManager:
    def __init__(self):
        self.projects = data.get('projects', {})
        self.running_processes = {}       # Popen objects
        self.project_logs = {}            # Log buffer per project
        self.terminal_ptys = {}           # PTY fds per project
        self.terminal_threads = {}        # Reader threads per project
        self._lock = threading.Lock()

    # ---------- CRUD ----------
    def create_project(self, name, command='python main.py'):
        project_id = str(uuid.uuid4())[:8]
        project_path = os.path.join(PROJECTS_DIR, project_id)
        os.makedirs(project_path, exist_ok=True)

        # Create template main.py
        with open(os.path.join(project_path, 'main.py'), 'w') as f:
            f.write('# NIROB PANEL - ' + name + '\n')
            f.write('print("🚀 Project running on NIROB PANEL!")\n')

        self.projects[project_id] = {
            'id': project_id,
            'name': name,
            'command': command,
            'status': 'stopped',
            'files': ['main.py'],
            'packages': [],
            'port': 8080,
            'created': str(datetime.now()),
            'main_file': 'main.py'
        }
        self.project_logs[project_id] = []
        data['projects'] = self.projects
        save_data(data)
        return project_id

    def get_all_projects(self):
        return list(self.projects.values())

    def get_project(self, project_id):
        return self.projects.get(project_id)

    def delete_project(self, project_id):
        with self._lock:
            if project_id not in self.projects:
                return False
            self.stop_project(project_id)
            project_path = os.path.join(PROJECTS_DIR, project_id)
            if os.path.exists(project_path):
                shutil.rmtree(project_path, ignore_errors=True)
            del self.projects[project_id]
            self.project_logs.pop(project_id, None)
            self.terminal_ptys.pop(project_id, None)
            self.terminal_threads.pop(project_id, None)
            data['projects'] = self.projects
            save_data(data)
            return True
        return False

    # ---------- Start / Stop (PTY-based) ----------
    def start_project(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        if project['status'] == 'running':
            return False, "Already running"

        project_path = os.path.join(PROJECTS_DIR, project_id)
        os.makedirs(project_path, exist_ok=True)

        # Install packages
        for pkg in project.get('packages', []):
            try:
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', pkg['name']],
                    cwd=project_path, check=True, capture_output=True, timeout=60
                )
            except Exception:
                pass

        try:
            command = project.get('command', 'python main.py')
            main_file = project.get('main_file', 'main.py')

            # Resolve command: if it references main.py, use the stored main_file
            cmd_parts = command.split()
            if cmd_parts and cmd_parts[-1] == 'main.py':
                cmd_parts[-1] = main_file
                command = ' '.join(cmd_parts)

            # Create a PTY for interactive terminal support
            master_fd, slave_fd = pty.openpty()

            process = subprocess.Popen(
                command.split() if not command.startswith('/') else [command],
                cwd=project_path,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid
            )
            os.close(slave_fd)

            with self._lock:
                self.running_processes[project_id] = process
                self.terminal_ptys[project_id] = master_fd
                project['status'] = 'running'
                data['projects'] = self.projects
                save_data(data)

            # Start PTY reader thread
            reader = threading.Thread(
                target=self._pty_reader,
                args=(project_id, process, master_fd),
                daemon=True
            )
            self.terminal_threads[project_id] = reader
            reader.start()

            # Start stdout reader as well (for logs via PIPE parallel)
            # We also start a subprocess with PIPE for log capture
            self._start_log_capture(project_id, command, project_path)

            return True, "Started successfully"

        except Exception as e:
            with self._lock:
                project['status'] = 'error'
                data['projects'] = self.projects
                save_data(data)
            return False, str(e)

    def _start_log_capture(self, project_id, command, project_path):
        """Start a parallel process just for log capture."""
        try:
            log_proc = subprocess.Popen(
                command.split(),
                cwd=project_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            # Store as secondary process
            with self._lock:
                self.running_processes[project_id + '_log'] = log_proc

            thread = threading.Thread(
                target=self._log_reader,
                args=(project_id, log_proc),
                daemon=True
            )
            thread.start()
        except Exception:
            pass

    def _pty_reader(self, project_id, process, master_fd):
        """Read from PTY and emit via SocketIO + log buffer."""
        try:
            while True:
                try:
                    output = os.read(master_fd, 1024)
                    if not output:
                        break
                    decoded = output.decode('utf-8', errors='replace')

                    # Store in log buffer
                    with self._lock:
                        if project_id not in self.project_logs:
                            self.project_logs[project_id] = []
                        for line in decoded.split('\n'):
                            if line.strip():
                                self.project_logs[project_id].append({
                                    'type': 'info',
                                    'message': line.strip(),
                                    'timestamp': datetime.now().strftime('%H:%M:%S')
                                })
                        # Trim to last 500 lines
                        if len(self.project_logs[project_id]) > 500:
                            self.project_logs[project_id] = self.project_logs[project_id][-500:]

                    # Emit via SocketIO for live terminal
                    socketio.emit('terminal_output', {
                        'project_id': project_id,
                        'data': decoded
                    })

                    # Also emit log update
                    socketio.emit('log_update', {
                        'project_id': project_id,
                        'data': decoded
                    })

                except OSError:
                    break
        except Exception:
            pass
        finally:
            try:
                os.close(master_fd)
            except Exception:
                pass

    def _log_reader(self, project_id, process):
        """Read stdout/stderr from log process."""
        try:
            for line in iter(process.stdout.readline, ''):
                if line:
                    with self._lock:
                        if project_id not in self.project_logs:
                            self.project_logs[project_id] = []
                        self.project_logs[project_id].append({
                            'type': 'info',
                            'message': f'[{datetime.now().strftime("%H:%M:%S")}] {line.strip()}'
                        })
                        if len(self.project_logs[project_id]) > 500:
                            self.project_logs[project_id] = self.project_logs[project_id][-500:]

                    socketio.emit('log_update', {
                        'project_id': project_id,
                        'data': line
                    })
        except Exception:
            pass

    def stop_project(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"

        errors = []

        # Kill main process
        if project_id in self.running_processes:
            try:
                proc = self.running_processes[project_id]
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            del self.running_processes[project_id]

        # Kill log process
        log_key = project_id + '_log'
        if log_key in self.running_processes:
            try:
                self.running_processes[log_key].terminate()
                self.running_processes[log_key].wait(timeout=3)
            except Exception:
                pass
            del self.running_processes[log_key]

        # Close PTY
        if project_id in self.terminal_ptys:
            try:
                os.close(self.terminal_ptys[project_id])
            except Exception:
                pass
            del self.terminal_ptys[project_id]

        project['status'] = 'stopped'
        data['projects'] = self.projects
        save_data(data)

        # Notify frontend
        socketio.emit('project_status', {
            'project_id': project_id,
            'status': 'stopped'
        })

        return True, "Stopped successfully"

    # ---------- Terminal Write ----------
    def terminal_write(self, project_id, data):
        """Write to the PTY (send command to the running process)."""
        if project_id in self.terminal_ptys:
            try:
                os.write(self.terminal_ptys[project_id], data.encode())
                return True
            except Exception:
                return False
        return False

    # ---------- Logs ----------
    def get_logs(self, project_id):
        return self.project_logs.get(project_id, [])

    # ---------- Files ----------
    def get_files(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return []
        project_path = os.path.join(PROJECTS_DIR, project_id)
        files = []
        if os.path.exists(project_path):
            for f in sorted(os.listdir(project_path)):
                filepath = os.path.join(project_path, f)
                if os.path.isfile(filepath):
                    ext = f.split('.')[-1].lower() if '.' in f else ''
                    files.append({
                        'name': f,
                        'size': os.path.getsize(filepath),
                        'ext': ext,
                        'modified': datetime.fromtimestamp(
                            os.path.getmtime(filepath)
                        ).strftime('%Y-%m-%d %H:%M:%S')
                    })
        return files

    def upload_file(self, project_id, file):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"

        project_path = os.path.join(PROJECTS_DIR, project_id)
        os.makedirs(project_path, exist_ok=True)

        filename = secure_filename(file.filename)
        if not filename:
            return False, "Invalid filename"

        filepath = os.path.join(project_path, filename)
        file.save(filepath)

        if filename not in project['files']:
            project['files'].append(filename)

        # Handle ZIP safely - NEVER overwrite existing files
        if filename.endswith('.zip'):
            try:
                with zipfile.ZipFile(filepath, 'r') as zip_ref:
                    # Safety: collect all entries first
                    entries = []
                    for info in zip_ref.infolist():
                        # Skip directory entries
                        if info.filename.endswith('/'):
                            continue
                        entries.append(info)

                    # Check for conflicts
                    conflicts = []
                    for info in entries:
                        target_path = os.path.join(project_path, info.filename)
                        if os.path.exists(target_path):
                            conflicts.append(info.filename)

                    if conflicts and not project.get('zip_overwrite_confirm'):
                        return (False,
                            f"CONFLICT_DETECTED:{','.join(conflicts[:10])}"
                        )

                    # Extract all entries
                    for info in entries:
                        target_path = os.path.join(project_path, info.filename)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        zip_ref.extract(info, project_path)

                    # Update file list
                    for info in entries:
                        fname = os.path.basename(info.filename)
                        if fname and fname not in project['files']:
                            project['files'].append(fname)

            except zipfile.BadZipFile:
                return False, "Invalid ZIP file"
            except Exception as e:
                return False, f"ZIP error: {str(e)}"

        # Handle requirements.txt
        if filename == 'requirements.txt':
            try:
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', filepath],
                    cwd=project_path, check=True, capture_output=True, timeout=120
                )
            except Exception:
                pass

        data['projects'] = self.projects
        save_data(data)
        return True, "File uploaded"

    def delete_file(self, project_id, filename):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        project_path = os.path.join(PROJECTS_DIR, project_id)
        filepath = os.path.join(project_path, filename)

        # Safety: prevent directory traversal
        if not os.path.abspath(filepath).startswith(os.path.abspath(project_path)):
            return False, "Invalid path"

        if os.path.exists(filepath) and os.path.isfile(filepath):
            os.remove(filepath)
        if filename in project['files']:
            project['files'].remove(filename)
        data['projects'] = self.projects
        save_data(data)
        return True, "File deleted"

    def get_file_content(self, project_id, filename):
        """Read file content for inline editing."""
        project = self.projects.get(project_id)
        if not project:
            return None
        project_path = os.path.join(PROJECTS_DIR, project_id)
        filepath = os.path.join(project_path, filename)

        if not os.path.abspath(filepath).startswith(os.path.abspath(project_path)):
            return None

        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            return None

        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception:
            return None

    def save_file_content(self, project_id, filename, content):
        """Save file content from inline editor."""
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        project_path = os.path.join(PROJECTS_DIR, project_id)
        filepath = os.path.join(project_path, filename)

        if not os.path.abspath(filepath).startswith(os.path.abspath(project_path)):
            return False, "Invalid path"

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            return True, "File saved"
        except Exception as e:
            return False, str(e)

    # ---------- Packages ----------
    def install_package(self, project_id, package_name):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"

        for pkg in project.get('packages', []):
            if pkg['name'] == package_name:
                return False, "Already installed"

        if 'packages' not in project:
            project['packages'] = []
        project['packages'].append({'name': package_name, 'version': 'latest'})

        project_path = os.path.join(PROJECTS_DIR, project_id)
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', package_name],
                cwd=project_path, check=True, capture_output=True, timeout=120
            )
        except subprocess.CalledProcessError as e:
            project['packages'] = [p for p in project['packages'] if p['name'] != package_name]
            data['projects'] = self.projects
            save_data(data)
            err_msg = e.stderr.decode() if e.stderr else str(e)
            return False, f"Install failed: {err_msg[:200]}"
        except Exception as e:
            return False, str(e)

        data['projects'] = self.projects
        save_data(data)
        return True, f"Package {package_name} installed"

    def uninstall_package(self, project_id, package_name):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        project['packages'] = [p for p in project.get('packages', []) if p['name'] != package_name]
        data['projects'] = self.projects
        save_data(data)
        return True, f"Package {package_name} uninstalled"

    # ---------- Settings ----------
    def update_settings(self, project_id, command, port, main_file):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        if command:
            project['command'] = command
        if port:
            project['port'] = int(port)
        if main_file:
            project['main_file'] = main_file
        data['projects'] = self.projects
        save_data(data)

        # Auto-restart if running
        if project['status'] == 'running':
            self.stop_project(project_id)
            self.start_project(project_id)

        return True, "Settings updated"

project_manager = ProjectManager()

# ============================================================
# ROUTES
# ============================================================

def login_required(f):
    """Decorator to require authentication."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    if 'username' in session:
        return render_template('index.html')
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    username = request.json.get('username', '').strip()
    password = request.json.get('password', '')
    user = data['users'].get(username)
    if user and verify_password(password, user['password']):
        session['username'] = username
        session['role'] = user.get('role', 'user')
        return jsonify({'success': True, 'role': session['role']})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/me')
@login_required
def get_me():
    return jsonify({
        'username': session['username'],
        'role': session['role']
    })

# ---------- Projects ----------
@app.route('/api/projects', methods=['GET'])
@login_required
def get_projects():
    return jsonify(project_manager.get_all_projects())

@app.route('/api/projects', methods=['POST'])
@login_required
def create_project():
    name = request.json.get('name', '').strip()
    command = request.json.get('command', 'python main.py')
    if not name:
        return jsonify({'error': 'Project name required'}), 400
    project_id = project_manager.create_project(name, command)
    return jsonify({'success': True, 'project_id': project_id})

@app.route('/api/projects/<project_id>', methods=['DELETE'])
@login_required
def delete_project(project_id):
    if project_manager.delete_project(project_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Project not found'}), 404

@app.route('/api/projects/<project_id>/start', methods=['POST'])
@login_required
def start_project(project_id):
    success, message = project_manager.start_project(project_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/stop', methods=['POST'])
@login_required
def stop_project(project_id):
    success, message = project_manager.stop_project(project_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/restart', methods=['POST'])
@login_required
def restart_project(project_id):
    project_manager.stop_project(project_id)
    success, message = project_manager.start_project(project_id)
    if success:
        return jsonify({'success': True, 'message': 'Restarted'})
    return jsonify({'error': message}), 400

# ---------- Files ----------
@app.route('/api/projects/<project_id>/files', methods=['GET'])
@login_required
def get_files(project_id):
    return jsonify(project_manager.get_files(project_id))

@app.route('/api/projects/<project_id>/upload', methods=['POST'])
@login_required
def upload_file(project_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    success, message = project_manager.upload_file(project_id, file)
    if success:
        return jsonify({'success': True, 'message': message})
    # Check for conflict detection
    if message and message.startswith('CONFLICT_DETECTED:'):
        conflicts = message.split(':', 1)[1].split(',')
        return jsonify({
            'success': False,
            'conflict': True,
            'message': 'The following files already exist: ' + ', '.join(conflicts),
            'conflicts': conflicts
        }), 409
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/files/<path:filename>', methods=['DELETE'])
@login_required
def delete_file(project_id, filename):
    success, message = project_manager.delete_file(project_id, filename)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/files/<path:filename>', methods=['GET'])
@login_required
def get_file(project_id, filename):
    content = project_manager.get_file_content(project_id, filename)
    if content is not None:
        return jsonify({'content': content, 'name': filename})
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/projects/<project_id>/files/<path:filename>', methods=['PUT'])
@login_required
def save_file(project_id, filename):
    content = request.json.get('content')
    if content is None:
        return jsonify({'error': 'No content provided'}), 400
    success, message = project_manager.save_file_content(project_id, filename, content)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

# ---------- Packages ----------
@app.route('/api/projects/<project_id>/packages', methods=['POST'])
@login_required
def install_package(project_id):
    package_name = request.json.get('name', '').strip()
    if not package_name:
        return jsonify({'error': 'Package name required'}), 400
    success, message = project_manager.install_package(project_id, package_name)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/packages/<package_name>', methods=['DELETE'])
@login_required
def uninstall_package(project_id, package_name):
    success, message = project_manager.uninstall_package(project_id, package_name)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

# ---------- Logs ----------
@app.route('/api/projects/<project_id>/logs', methods=['GET'])
@login_required
def get_logs(project_id):
    logs = project_manager.get_logs(project_id)
    return jsonify(logs)

# ---------- Settings ----------
@app.route('/api/projects/<project_id>/settings', methods=['POST'])
@login_required
def update_settings(project_id):
    command = request.json.get('command')
    port = request.json.get('port')
    main_file = request.json.get('main_file')
    success, message = project_manager.update_settings(project_id, command, port, main_file)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

# ---------- Users ----------
@app.route('/api/users', methods=['GET'])
@login_required
def get_users():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    users = []
    for username, info in data['users'].items():
        users.append({
            'username': username,
            'email': info.get('email', ''),
            'role': info.get('role', 'user'),
            'created': info.get('created', '')
        })
    return jsonify(users)

@app.route('/api/users/<username>', methods=['DELETE'])
@login_required
def delete_user(username):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    if username == 'xNIROB':
        return jsonify({'error': 'Cannot delete admin'}), 400
    if username in data['users']:
        del data['users'][username]
        save_data(data)
        return jsonify({'success': True})
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/register', methods=['POST'])
def register():
    username = request.json.get('username', '').strip()
    email = request.json.get('email', '').strip()
    password = request.json.get('password', '')

    if not username or not email or not password:
        return jsonify({'error': 'All fields required'}), 400
    if len(username) < 3 or len(username) > 32:
        return jsonify({'error': 'Username must be 3-32 characters'}), 400
    if len(password) < 4:
        return jsonify({'error': 'Password must be at least 4 characters'}), 400
    if username in data['users']:
        return jsonify({'error': 'Username exists'}), 400

    data['users'][username] = {
        'password': hash_password(password),
        'email': email,
        'role': 'user',
        'created': str(datetime.now())
    }
    save_data(data)
    return jsonify({'success': True})

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    current_password = request.json.get('current_password', '')
    new_password = request.json.get('new_password', '')
    username = session['username']
    user = data['users'].get(username)

    if not user or not verify_password(current_password, user['password']):
        return jsonify({'error': 'Current password incorrect'}), 400
    if len(new_password) < 4:
        return jsonify({'error': 'New password must be at least 4 characters'}), 400

    data['users'][username]['password'] = hash_password(new_password)
    save_data(data)
    return jsonify({'success': True})

# ---------- Maintenance ----------
@app.route('/api/maintenance', methods=['POST'])
@login_required
def toggle_maintenance():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data['maintenance'] = not data.get('maintenance', False)
    save_data(data)
    return jsonify({'maintenance': data['maintenance']})

@app.route('/api/maintenance', methods=['GET'])
def get_maintenance():
    return jsonify({'maintenance': data.get('maintenance', False)})

# ---------- Site Settings ----------
@app.route('/api/site_name', methods=['POST'])
@login_required
def update_site_name():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    name = request.json.get('name', '').strip()
    if name:
        data['site_name'] = name
        save_data(data)
        return jsonify({'success': True})
    return jsonify({'error': 'Name required'}), 400

@app.route('/api/site_name', methods=['GET'])
def get_site_name():
    return jsonify({'name': data.get('site_name', 'NIROB PANEL')})

# ---------- System Info ----------
@app.route('/api/system/stats')
def system_stats():
    import psutil
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        return jsonify({
            'cpu': round(cpu, 1),
            'ram': round(ram, 1),
            'disk': round(disk, 1)
        })
    except ImportError:
        return jsonify({
            'cpu': 0,
            'ram': 0,
            'disk': 0
        })

# ============================================================
# SOCKET.IO EVENTS (Live Terminal)
# ============================================================

@socketio.on('connect')
def handle_connect():
    emit('connected', {'message': 'Connected to NIROB Panel live stream'})

@socketio.on('terminal_input')
def handle_terminal_input(data):
    """Receive terminal input from browser and write to PTY."""
    project_id = data.get('project_id')
    input_data = data.get('data', '')
    if project_id:
        project_manager.terminal_write(project_id, input_data)

@socketio.on('subscribe_project')
def handle_subscribe(data):
    """Client subscribes to project updates."""
    project_id = data.get('project_id')
    if project_id:
        # Send existing logs on subscribe
        logs = project_manager.get_logs(project_id)
        for log_entry in logs:
            emit('log_update', {
                'project_id': project_id,
                'data': log_entry.get('message', '') + '\n'
            })

# ============================================================
# RUN
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))

    # Try to install psutil optionally
    try:
        import psutil
    except ImportError:
        print("[INFO] psutil not installed. System stats will show 0.")

    print(f"""
    ╔══════════════════════════════════════════╗
    ║       NIROB PANEL v6.0 ENTERPRISE        ║
    ║     Running on port {port}                   ║
    ║  Live Terminal, Logs, File Editor Ready  ║
    ╚══════════════════════════════════════════╝
    """)

    socketio.run(app, host='0.0.0.0', port=port, debug=False)
