# ============================================================
# main.py - NIROB PANEL
# Version: 5.0 VIP
# ============================================================

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO
import os
import json
import subprocess
import threading
import uuid
import shutil
import zipfile
import sys
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'nirob-vip-secret-key-2026'
socketio = SocketIO(app, cors_allowed_origins="*")

# ============================================================
# DATA
# ============================================================
DATA_FILE = 'data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return default_data()

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def default_data():
    return {
        'users': {
            'xNIROB': {'password': 'Hosting', 'role': 'admin', 'created': str(datetime.now())}
        },
        'projects': {},
        'system_logs': [],
        'maintenance': False,
        'site_name': 'NIROB PANEL'
    }

data = load_data()
os.makedirs('projects', exist_ok=True)

# ============================================================
# PROJECT MANAGER
# ============================================================
class ProjectManager:
    def __init__(self):
        self.projects = data.get('projects', {})
        self.running_processes = {}
        self.project_logs = {}
    
    def create_project(self, name, command='python main.py'):
        project_id = str(uuid.uuid4())[:8]
        project_path = os.path.join('projects', project_id)
        os.makedirs(project_path, exist_ok=True)
        
        with open(os.path.join(project_path, 'main.py'), 'w') as f:
            f.write('# NIROB PANEL - Your Project\n')
            f.write('print("🚀 Project running on NIROB PANEL!")\n')
            f.write('print("✨ Server is ready!")\n')
        
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
        if project_id in self.projects:
            self.stop_project(project_id)
            project_path = os.path.join('projects', project_id)
            if os.path.exists(project_path):
                shutil.rmtree(project_path)
            del self.projects[project_id]
            if project_id in self.project_logs:
                del self.project_logs[project_id]
            data['projects'] = self.projects
            save_data(data)
            return True
        return False
    
    def start_project(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        if project['status'] == 'running':
            return False, "Already running"
        
        project_path = os.path.join('projects', project_id)
        if not os.path.exists(project_path):
            os.makedirs(project_path, exist_ok=True)
        
        # Install packages
        for pkg in project.get('packages', []):
            try:
                subprocess.run([sys.executable, '-m', 'pip', 'install', pkg['name']],
                             cwd=project_path, check=True, capture_output=True)
            except:
                pass
        
        try:
            command = project.get('command', 'python main.py')
            process = subprocess.Popen(
                command.split(),
                cwd=project_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            self.running_processes[project_id] = process
            project['status'] = 'running'
            data['projects'] = self.projects
            save_data(data)
            
            thread = threading.Thread(target=self.read_logs, args=(project_id, process))
            thread.daemon = True
            thread.start()
            return True, "Started successfully"
        except Exception as e:
            project['status'] = 'error'
            data['projects'] = self.projects
            save_data(data)
            return False, str(e)
    
    def stop_project(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        
        if project_id in self.running_processes:
            try:
                process = self.running_processes[project_id]
                process.terminate()
                process.wait(timeout=5)
            except:
                try:
                    process.kill()
                except:
                    pass
            del self.running_processes[project_id]
        
        project['status'] = 'stopped'
        data['projects'] = self.projects
        save_data(data)
        return True, "Stopped successfully"
    
    def read_logs(self, project_id, process):
        for line in iter(process.stdout.readline, ''):
            if line:
                if project_id not in self.project_logs:
                    self.project_logs[project_id] = []
                self.project_logs[project_id].append({
                    'type': 'info',
                    'message': f'[{datetime.now().strftime("%H:%M:%S")}] {line.strip()}'
                })
                if len(self.project_logs[project_id]) > 200:
                    self.project_logs[project_id] = self.project_logs[project_id][-200:]
    
    def get_logs(self, project_id):
        return self.project_logs.get(project_id, [])
    
    def get_files(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            return []
        project_path = os.path.join('projects', project_id)
        files = []
        if os.path.exists(project_path):
            for f in os.listdir(project_path):
                filepath = os.path.join(project_path, f)
                if os.path.isfile(filepath):
                    ext = f.split('.')[-1].lower() if '.' in f else ''
                    files.append({
                        'name': f,
                        'size': os.path.getsize(filepath),
                        'ext': ext,
                        'modified': datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%Y-%m-%d %H:%M:%S')
                    })
        return files
    
    def upload_file(self, project_id, file):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        
        project_path = os.path.join('projects', project_id)
        os.makedirs(project_path, exist_ok=True)
        filename = file.filename
        filepath = os.path.join(project_path, filename)
        file.save(filepath)
        
        if filename not in project['files']:
            project['files'].append(filename)
        
        # Handle ZIP
        if filename.endswith('.zip'):
            try:
                with zipfile.ZipFile(filepath, 'r') as zip_ref:
                    zip_ref.extractall(project_path)
                for root, dirs, files in os.walk(project_path):
                    for f in files:
                        if f not in project['files']:
                            project['files'].append(f)
            except:
                pass
        
        # Handle requirements.txt
        if filename == 'requirements.txt':
            try:
                subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', filepath],
                             cwd=project_path, check=True)
            except:
                pass
        
        data['projects'] = self.projects
        save_data(data)
        return True, "File uploaded"
    
    def delete_file(self, project_id, filename):
        project = self.projects.get(project_id)
        if not project:
            return False, "Project not found"
        project_path = os.path.join('projects', project_id)
        filepath = os.path.join(project_path, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        if filename in project['files']:
            project['files'].remove(filename)
        data['projects'] = self.projects
        save_data(data)
        return True, "File deleted"
    
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
        
        # Install immediately
        project_path = os.path.join('projects', project_id)
        try:
            subprocess.run([sys.executable, '-m', 'pip', 'install', package_name],
                         cwd=project_path, check=True, capture_output=True)
        except:
            pass
        
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
        return True, "Settings updated"

project_manager = ProjectManager()

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    if 'username' in session:
        return render_template('index.html')
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    username = request.json.get('username')
    password = request.json.get('password')
    user = data['users'].get(username)
    if user and user['password'] == password:
        session['username'] = username
        session['role'] = user.get('role', 'user')
        return jsonify({'success': True, 'role': session['role']})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/projects', methods=['GET'])
def get_projects():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(project_manager.get_all_projects())

@app.route('/api/projects', methods=['POST'])
def create_project():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    name = request.json.get('name')
    command = request.json.get('command', 'python main.py')
    if not name:
        return jsonify({'error': 'Project name required'}), 400
    project_id = project_manager.create_project(name, command)
    return jsonify({'success': True, 'project_id': project_id})

@app.route('/api/projects/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if project_manager.delete_project(project_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Project not found'}), 404

@app.route('/api/projects/<project_id>/start', methods=['POST'])
def start_project(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    success, message = project_manager.start_project(project_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/stop', methods=['POST'])
def stop_project(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    success, message = project_manager.stop_project(project_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/files', methods=['GET'])
def get_files(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(project_manager.get_files(project_id))

@app.route('/api/projects/<project_id>/upload', methods=['POST'])
def upload_file(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    success, message = project_manager.upload_file(project_id, file)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/files/<filename>', methods=['DELETE'])
def delete_file(project_id, filename):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    success, message = project_manager.delete_file(project_id, filename)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/packages', methods=['POST'])
def install_package(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    package_name = request.json.get('name')
    if not package_name:
        return jsonify({'error': 'Package name required'}), 400
    success, message = project_manager.install_package(project_id, package_name)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/packages/<package_name>', methods=['DELETE'])
def uninstall_package(project_id, package_name):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    success, message = project_manager.uninstall_package(project_id, package_name)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/projects/<project_id>/logs', methods=['GET'])
def get_logs(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(project_manager.get_logs(project_id))

@app.route('/api/projects/<project_id>/settings', methods=['POST'])
def update_settings(project_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    command = request.json.get('command')
    port = request.json.get('port')
    main_file = request.json.get('main_file')
    success, message = project_manager.update_settings(project_id, command, port, main_file)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 400

@app.route('/api/users', methods=['GET'])
def get_users():
    if 'username' not in session or session.get('role') != 'admin':
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
def delete_user(username):
    if 'username' not in session or session.get('role') != 'admin':
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
    username = request.json.get('username')
    email = request.json.get('email')
    password = request.json.get('password')
    if not username or not email or not password:
        return jsonify({'error': 'All fields required'}), 400
    if username in data['users']:
        return jsonify({'error': 'Username exists'}), 400
    data['users'][username] = {
        'password': password,
        'email': email,
        'role': 'user',
        'created': str(datetime.now())
    }
    save_data(data)
    return jsonify({'success': True})

@app.route('/api/maintenance', methods=['POST'])
def toggle_maintenance():
    if 'username' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data['maintenance'] = not data.get('maintenance', False)
    save_data(data)
    return jsonify({'maintenance': data['maintenance']})

@app.route('/api/maintenance', methods=['GET'])
def get_maintenance():
    return jsonify({'maintenance': data.get('maintenance', False)})

@app.route('/api/site_name', methods=['POST'])
def update_site_name():
    if 'username' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    name = request.json.get('name')
    if name:
        data['site_name'] = name
        save_data(data)
        return jsonify({'success': True})
    return jsonify({'error': 'Name required'}), 400

@app.route('/api/site_name', methods=['GET'])
def get_site_name():
    return jsonify({'name': data.get('site_name', 'NIROB PANEL')})

@app.route('/api/change_password', methods=['POST'])
def change_password():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    current_password = request.json.get('current_password')
    new_password = request.json.get('new_password')
    username = session['username']
    user = data['users'].get(username)
    if not user or user['password'] != current_password:
        return jsonify({'error': 'Current password incorrect'}), 400
    data['users'][username]['password'] = new_password
    save_data(data)
    return jsonify({'success': True})

# ============================================================
# RUN
# ============================================================
if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)