import json
import os
import posixpath
import shlex
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from frappe_auto_deploy.deploy import BENCH_NODE_MAP, _with_sudo, normalize_node_version, with_nvm
from frappe_auto_deploy.executor import Executor


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = PROJECT_ROOT / "server_profile.json"
WEB_ROOT = PROJECT_ROOT / "web"


def shell_quote(value):
    return shlex.quote(str(value))


def with_configured_sudo(command, config):
    access_mode = config.get("access_mode", "root")
    ssh_user = str(config.get("ssh_user", "root")).strip()
    is_root_user = access_mode == "root" or ssh_user == "root"
    sudo_password = config.get("sudo_password") or config.get("ssh_password")
    return _with_sudo(command, sudo_password=sudo_password, is_root=is_root_user)


def load_profile():
    if not PROFILE_PATH.exists():
        return {
            "remote": False,
            "use_wsl": False,
            "access_mode": "root",
            "server_ip": "",
            "ssh_port": 22,
            "ssh_user": "",
            "ssh_password": "",
            "use_ssh_key": False,
            "ssh_private_key": "",
            "ssh_key_passphrase": "",
            "ssh_public_key": "",
            "sudo_password": "",
            "bench_version": "version-15",
            "use_nvm": True,
            "site_admin_password": "admin",
            "mariadb_root_password": "root",
        }
    with open(PROFILE_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("use_wsl", False)
    data.setdefault("ssh_port", 22)
    data.setdefault("use_ssh_key", False)
    data.setdefault("ssh_private_key", "")
    data.setdefault("ssh_key_passphrase", "")
    data.setdefault("ssh_public_key", "")
    data.setdefault("bench_version", "version-15")
    data.setdefault("use_nvm", True)
    data.setdefault("site_admin_password", "admin")
    data.setdefault("mariadb_root_password", "root")
    return data


def save_profile(profile):
    merged = load_profile()
    merged.update(profile or {})
    with open(PROFILE_PATH, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
    return merged


def validate_connection_config(config):
    if config.get("remote"):
        if not config.get("server_ip"):
            return "Server IP is required."
        if config.get("use_ssh_key") and not config.get("ssh_private_key"):
            return "SSH private key is required when SSH key login is enabled."
        if config.get("use_ssh_key") and str(config.get("ssh_private_key", "")).startswith("ssh-"):
            return "That looks like a public key. SSH key login needs the private key that starts with `-----BEGIN`."
        if not config.get("use_ssh_key") and not config.get("ssh_password"):
            return "SSH password is required."
        if config.get("access_mode") == "user" and not config.get("ssh_user"):
            return "SSH username is required for user access mode."
        if config.get("access_mode") == "user" and not config.get("sudo_password"):
            return "Sudo Password is required for user access mode."
    return ""


def build_executor(config):
    return Executor(
        remote=config.get("remote"),
        host=config.get("server_ip"),
        port=int(config.get("ssh_port", 22)),
        username=config.get("ssh_user"),
        password=config.get("ssh_password"),
        use_wsl=config.get("use_wsl", False),
        use_ssh_key=config.get("use_ssh_key", False),
        ssh_private_key=config.get("ssh_private_key", ""),
        ssh_key_passphrase=config.get("ssh_key_passphrase", ""),
    )


def _node_version_for_config(config):
    bench_version = config.get("bench_version") or "version-15"
    return normalize_node_version(BENCH_NODE_MAP.get(bench_version, "18"))


def _run_command(config, command, use_nvm=False):
    if use_nvm and config.get("use_nvm", True):
        command = with_nvm(command, _node_version_for_config(config))
    command = with_configured_sudo(command, config)
    executor = build_executor(config)
    try:
        code, out, err = executor.run(command, check=False)
        if code != 0:
            raise RuntimeError(err.strip() or out.strip() or f"Command failed with exit code {code}.")
        return {"ok": True, "stdout": out.strip(), "stderr": err.strip()}
    finally:
        executor.close()


def discover_benches(config):
    executor = build_executor(config)
    try:
        command = (
            'for root_path in "$HOME" /home /opt /var/www; do '
            '[ -d "$root_path" ] || continue; '
            'find "$root_path" -mindepth 1 -maxdepth 4 -type d -name sites 2>/dev/null; '
            'done | sort -u | while IFS= read -r sites_path; do '
            'bench_path="$(dirname "$sites_path")"; '
            '[ -d "$bench_path/apps/frappe" ] || [ -f "$sites_path/common_site_config.json" ] || continue; '
            'bench_name="$(basename "$bench_path")"; '
            'printf "BENCH|%s|%s\\n" "$bench_name" "$bench_path"; '
            'if [ -x "$HOME/.local/bin/bench" ]; then export PATH="$HOME/.local/bin:$PATH"; fi; '
            'site_names="$(cd "$bench_path" && bench list-sites 2>/dev/null || true)"; '
            'if [ -n "$site_names" ]; then '
            'printf "%s\n" "$site_names" | while IFS= read -r site_name; do '
            '[ -n "$site_name" ] || continue; '
            '[ "$site_name" = "assets" ] && continue; '
            'printf "SITE|%s|%s|%s\\n" "$bench_name" "$bench_path" "$site_name"; '
            'done; '
            'else '
            'for site_path in "$sites_path"/*; do '
            '[ -d "$site_path" ] || continue; '
            'site_name="$(basename "$site_path")"; '
            '[ "$site_name" = "assets" ] && continue; '
            '[ -f "$site_path/site_config.json" ] || continue; '
            'printf "SITE|%s|%s|%s\\n" "$bench_name" "$bench_path" "$site_name"; '
            'done; '
            'fi; '
            "done; "
            "done"
        )
        code, out, err = executor.run(command, check=False)
        if code != 0:
            raise RuntimeError(err.strip() or out.strip() or "Unable to load benches from the server.")
        benches = {}
        for raw_line in out.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("|")
            if parts[0] == "BENCH" and len(parts) >= 3:
                _, bench_name, bench_path = parts[:3]
                benches[(bench_name, bench_path)] = {
                    "bench": bench_name,
                    "bench_path": bench_path,
                    "site_count": 0,
                    "sites": [],
                }
            elif parts[0] == "SITE" and len(parts) >= 4:
                _, bench_name, bench_path, site_name = parts[:4]
                payload = benches.setdefault(
                    (bench_name, bench_path),
                    {"bench": bench_name, "bench_path": bench_path, "site_count": 0, "sites": []},
                )
                payload["sites"].append(site_name)
        items = sorted(benches.values(), key=lambda entry: (entry["bench"], entry["bench_path"]))
        for item in items:
            item["sites"].sort()
            item["site_count"] = len(item["sites"])
        return items
    finally:
        executor.close()


def create_site(config, payload):
    bench_path = (payload.get("bench_path") or "").strip()
    site_name = (payload.get("site_name") or "").strip()
    admin_password = (payload.get("site_admin_password") or config.get("site_admin_password") or "admin").strip()
    mariadb_root_password = (payload.get("mariadb_root_password") or config.get("mariadb_root_password") or "root").strip()
    if not bench_path:
        raise ValueError("Bench is required.")
    if not site_name:
        raise ValueError("Site name is required.")

    command = (
        f"cd {shell_quote(bench_path)} && "
        f"if bench --site {shell_quote(site_name)} list-apps >/dev/null 2>&1; then "
        f"echo 'Site already exists: {site_name}'; "
        "else "
        f"bench new-site {shell_quote(site_name)} --admin-password {shell_quote(admin_password)} --mariadb-root-password {shell_quote(mariadb_root_password)}; "
        "fi"
    )
    return _run_command(config, command, use_nvm=True)


def migrate_site(config, payload):
    bench_path = (payload.get("bench_path") or "").strip()
    site_name = (payload.get("site_name") or "").strip()
    if not bench_path or not site_name:
        raise ValueError("Bench and site are required for migrate.")
    command = f"cd {shell_quote(bench_path)} && bench --site {shell_quote(site_name)} migrate"
    return _run_command(config, command, use_nvm=True)


def restore_site(config, payload):
    bench_path = (payload.get("bench_path") or "").strip()
    site_name = (payload.get("site_name") or "").strip()
    db_backup_path = (payload.get("db_backup_path") or "").strip()
    public_backup_path = (payload.get("public_backup_path") or "").strip()
    private_backup_path = (payload.get("private_backup_path") or "").strip()
    if not bench_path or not site_name:
        raise ValueError("Bench and site are required for restore.")
    if not db_backup_path:
        raise ValueError("Database backup path is required.")
    restore_parts = [
        f"cd {shell_quote(bench_path)}",
        f"bench --site {shell_quote(site_name)} --force restore {shell_quote(db_backup_path)}",
    ]
    if public_backup_path:
        restore_parts.append(f"bench --site {shell_quote(site_name)} restore-files --only public {shell_quote(public_backup_path)}")
    if private_backup_path:
        restore_parts.append(f"bench --site {shell_quote(site_name)} restore-files --only private {shell_quote(private_backup_path)}")
    restore_parts.append(f"bench --site {shell_quote(site_name)} migrate")
    command = " && ".join(restore_parts)
    return _run_command(config, command, use_nvm=True)


def _github_repo_with_auth(repo_url, username, token):
    repo_url = str(repo_url or "").strip()
    username = str(username or "").strip()
    token = str(token or "").strip()
    if not repo_url:
        raise ValueError("Repository URL is required.")
    if not token:
        return repo_url
    if repo_url.startswith("https://github.com/"):
        auth_user = username or "oauth2"
        return repo_url.replace("https://", f"https://{auth_user}:{token}@")
    return repo_url


def install_github_app(config, payload):
    bench_path = (payload.get("bench_path") or "").strip()
    repo_url = (payload.get("repo_url") or "").strip()
    branch = (payload.get("branch") or "").strip()
    app_name = (payload.get("app_name") or "").strip()
    site_name = (payload.get("site_name") or "").strip()
    install_on_site = bool(payload.get("install_on_site"))
    if not bench_path:
        raise ValueError("Bench is required for app install.")
    repo_url = _github_repo_with_auth(repo_url, payload.get("github_username"), payload.get("github_token"))
    get_app_cmd = f"cd {shell_quote(bench_path)} && bench get-app "
    if app_name:
        get_app_cmd += f"{shell_quote(app_name)} "
    get_app_cmd += shell_quote(repo_url)
    if branch:
        get_app_cmd += f" --branch {shell_quote(branch)}"
    if install_on_site:
        if not site_name:
            raise ValueError("Site is required when Install On Site is enabled.")
        target_app = app_name or posixpath.splitext(posixpath.basename(repo_url.rstrip("/")))[0]
        get_app_cmd += f" && bench --site {shell_quote(site_name)} install-app {shell_quote(target_app)}"
    return _run_command(config, get_app_cmd, use_nvm=True)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "FrappeAutoDeployWeb/1.0"

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/plain; charset=utf-8"
        if path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/profile":
            self._send_json({"profile": load_profile()})
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file(WEB_ROOT / "index.html")
            return
        asset_path = (WEB_ROOT / parsed.path.lstrip("/")).resolve()
        if WEB_ROOT.resolve() not in asset_path.parents and asset_path != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self._send_file(asset_path)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/profile":
                profile = save_profile(payload.get("profile", {}))
                self._send_json({"profile": profile})
                return
            if parsed.path == "/api/benches/discover":
                config = payload.get("config") or load_profile()
                error = validate_connection_config(config)
                if error:
                    self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                    return
                benches = discover_benches(config)
                self._send_json({"benches": benches})
                return
            if parsed.path == "/api/sites/create":
                config = payload.get("config") or load_profile()
                error = validate_connection_config(config)
                if error:
                    self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                    return
                result = create_site(config, payload)
                self._send_json(result)
                return
            if parsed.path == "/api/sites/migrate":
                config = payload.get("config") or load_profile()
                error = validate_connection_config(config)
                if error:
                    self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(migrate_site(config, payload))
                return
            if parsed.path == "/api/sites/restore":
                config = payload.get("config") or load_profile()
                error = validate_connection_config(config)
                if error:
                    self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(restore_site(config, payload))
                return
            if parsed.path == "/api/apps/install-github":
                config = payload.get("config") or load_profile()
                error = validate_connection_config(config)
                if error:
                    self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(install_github_app(config, payload))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def run_web_app(host="127.0.0.1", port=8787, open_browser=False):
    server = ThreadingHTTPServer((host, port), WebHandler)
    url = f"http://{host}:{port}"
    print(f"Vue web app running at {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
