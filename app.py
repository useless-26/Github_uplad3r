from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import zipfile
import io
import base64
import requests
import time
import os
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

GH_API = "https://api.github.com"

def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/connect", methods=["POST"])
def connect():
    data = request.get_json()
    token = (data or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "Token is required"}), 400

    r = requests.get(f"{GH_API}/user", headers=gh_headers(token))
    if not r.ok:
        msg = r.json().get("message", "Authentication failed")
        return jsonify({"error": msg}), 401

    user = r.json()

    repos = []
    page = 1
    while True:
        rr = requests.get(
            f"{GH_API}/user/repos?per_page=100&page={page}&sort=updated",
            headers=gh_headers(token)
        )
        if not rr.ok:
            break
        batch = rr.json()
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return jsonify({
        "user": {
            "login": user["login"],
            "name": user.get("name") or user["login"],
            "avatar_url": user["avatar_url"]
        },
        "repos": [
            {"name": r["name"], "private": r["private"], "updated_at": r["updated_at"]}
            for r in repos
        ]
    })

def create_folder_recursive(owner, repo_name, folder_path, token, logs, created_cache):
    if not folder_path or folder_path.endswith('/'):
        folder_path = folder_path.rstrip('/')
    
    if not folder_path:
        return True
    
    cache_key = f"{owner}/{repo_name}/{folder_path}"
    if cache_key in created_cache:
        return True
    
    if '/' in folder_path:
        parent = '/'.join(folder_path.split('/')[:-1])
        if parent:
            if not create_folder_recursive(owner, repo_name, parent, token, logs, created_cache):
                return False
    
    check_url = f"{GH_API}/repos/{owner}/{repo_name}/contents/{quote(folder_path)}"
    hdrs = gh_headers(token)
    r = requests.get(check_url, headers=hdrs)
    
    if r.status_code == 200:
        created_cache.add(cache_key)
        return True
    
    gitkeep_path = f"{folder_path}/.gitkeep"
    content_b64 = base64.b64encode(b"").decode()
    
    payload = {
        "message": f"Create folder {folder_path}",
        "content": content_b64
    }
    
    put_url = f"{GH_API}/repos/{owner}/{repo_name}/contents/{quote(gitkeep_path)}"
    response = requests.put(put_url, json=payload, headers=hdrs)
    
    if response.ok:
        logs.append({"type": "info", "text": f"Created folder: {folder_path}/"})
        created_cache.add(cache_key)
        return True
    return False

@app.route("/api/push", methods=["POST"])
def push():
    token = request.form.get("token", "").strip()
    mode = request.form.get("mode", "existing")
    repo_name = request.form.get("repo_name", "").strip()
    private = request.form.get("private", "false") == "true"
    zip_file = request.files.get("zip_file")

    if not token:
        return jsonify({"error": "Missing token"}), 400
    if not repo_name:
        return jsonify({"error": "Missing repo name"}), 400
    if not zip_file:
        return jsonify({"error": "No ZIP file provided"}), 400

    hdrs = gh_headers(token)
    logs = []

    user_r = requests.get(f"{GH_API}/user", headers=hdrs)
    if not user_r.ok:
        return jsonify({"error": "Invalid token"}), 401
    owner = user_r.json()["login"]

    def log(t, text):
        logs.append({"type": t, "text": text})

    def push_file(path, content_b64, message, created_cache):
        if '/' in path:
            parent_dir = '/'.join(path.split('/')[:-1])
            if parent_dir:
                if not create_folder_recursive(owner, repo_name, parent_dir, token, logs, created_cache):
                    return False, "Failed to create parent folder"
        
        sha = None
        encoded_path = quote(path)
        ex = requests.get(
            f"{GH_API}/repos/{owner}/{repo_name}/contents/{encoded_path}",
            headers=hdrs
        )
        if ex.status_code == 200:
            sha = ex.json().get("sha")

        payload = {"message": message, "content": content_b64}
        if sha:
            payload["sha"] = sha

        r = requests.put(
            f"{GH_API}/repos/{owner}/{repo_name}/contents/{encoded_path}",
            json=payload,
            headers=hdrs
        )
        
        if r.status_code in [200, 201]:
            return True, None
        else:
            error_msg = r.json().get("message", "Unknown error")
            return False, error_msg

    if mode == "new":
        log("info", f'Creating repository "{repo_name}"...')
        r = requests.post(
            f"{GH_API}/user/repos",
            json={"name": repo_name, "private": private, "auto_init": False},
            headers=hdrs
        )
        if not r.ok:
            error_detail = r.json().get("message", "Repo creation failed")
            return jsonify({"error": error_detail}), 400
        log("success", f'Repository "{repo_name}" created')
        time.sleep(1.5)

    verify_url = f"{GH_API}/repos/{owner}/{repo_name}"
    verify_resp = requests.get(verify_url, headers=hdrs)
    if not verify_resp.ok:
        return jsonify({"error": f"Repository '{repo_name}' not found"}), 404

    total_files_pushed = 0
    created_folders_cache = set()

    zip_bytes = zip_file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid ZIP file"}), 400

    file_list = [info for info in zf.infolist() if not info.is_dir()]
    
    if not file_list:
        return jsonify({"error": "ZIP file contains no files"}), 400
    
    log("info", f"Found {len(file_list)} file(s) in ZIP")
    
    file_list.sort(key=lambda x: x.filename)
    
    for file_info in file_list:
        file_path = file_info.filename
        if file_path.startswith('./'):
            file_path = file_path[2:]
        if file_path.startswith('/'):
            file_path = file_path[1:]
        
        if not file_path or file_path.endswith('/'):
            continue
        
        try:
            file_content = zf.read(file_info)
            content_b64 = base64.b64encode(file_content).decode()
            
            log("info", f"Uploading: {file_path}")
            success, error = push_file(file_path, content_b64, f"Add {file_path}", created_folders_cache)
            
            if success:
                log("success", f"✓ {file_path}")
                total_files_pushed += 1
            else:
                log("error", f"✗ {file_path} - {error}")
                
        except Exception as e:
            log("error", f"✗ {file_path} - {str(e)}")
    
    log("success", f"Pushed {total_files_pushed} file(s)")

    repo_url = f"https://github.com/{owner}/{repo_name}"

    return jsonify({
        "logs": logs,
        "repo_url": repo_url,
        "owner": owner,
        "total_files": total_files_pushed
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port, host='0.0.0.0')