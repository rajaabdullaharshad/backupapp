import json
import os
import platform
import posixpath
import re
import shlex
import sys
import traceback

from PySide6 import QtCore, QtGui, QtWidgets

from frappe_auto_deploy.deploy import BENCH_NODE_MAP, _with_sudo, deserialize_recovery_commands, execute_deployment, execute_resume_deployment, normalize_node_version


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MANUAL_CWD_MARKER_RE = re.compile(r"^__FAD_PWD__=(.*)$", re.MULTILINE)


def shell_quote(value):
    return shlex.quote(str(value))


def with_configured_sudo(command, config):
    access_mode = config.get("access_mode", "root")
    ssh_user = str(config.get("ssh_user", "root")).strip()
    is_root_user = access_mode == "root" or ssh_user == "root"
    sudo_password = config.get("sudo_password") or config.get("ssh_password")
    return _with_sudo(command, sudo_password=sudo_password, is_root=is_root_user)


class TerminalConsole(QtWidgets.QPlainTextEdit):
    command_submitted = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.prompt = "$ "
        self._input_start = None
        self.setUndoRedoEnabled(False)

    def reset_terminal(self):
        self.setPlainText("")
        self._input_start = None
        self.enable_prompt()

    def enable_prompt(self, text=""):
        self.setReadOnly(False)
        content = self.toPlainText()
        if self._input_start is None:
            if content and not content.endswith("\n"):
                content += "\n"
            content += self.prompt + text
        else:
            prompt_start = self._input_start - len(self.prompt)
            content = content[:prompt_start] + self.prompt + text
        self.setPlainText(content)
        self._input_start = len(content) - len(text)
        self._move_cursor_to_end()

    def disable_prompt(self):
        if self._input_start is None:
            self.setReadOnly(True)
            return
        content = self.toPlainText()
        if content and not content.endswith("\n"):
            content += "\n"
        self.setPlainText(content)
        self._input_start = None
        self.setReadOnly(True)
        self._move_cursor_to_end()

    def set_current_command(self, text):
        self.enable_prompt(text)

    def set_prompt(self, prompt):
        prompt = str(prompt or "$ ")
        current_input = self.current_command() if self._input_start is not None else ""
        self.prompt = prompt
        if self._input_start is not None:
            self.enable_prompt(current_input)

    def current_command(self):
        if self._input_start is None:
            return ""
        return self.toPlainText()[self._input_start:]

    def append_output(self, text):
        text = str(text or "").rstrip("\n")
        if not text:
            return
        content = self.toPlainText()
        current_input = ""
        if self._input_start is None:
            base = content.rstrip("\n")
        else:
            prompt_start = self._input_start - len(self.prompt)
            base = content[:prompt_start].rstrip("\n")
            current_input = content[self._input_start:]
        new_content = "\n".join(part for part in (base, text) if part)
        if self._input_start is not None:
            new_content = "\n".join(part for part in (new_content, self.prompt + current_input) if part)
            self._input_start = len(new_content) - len(current_input)
        self.setPlainText(new_content)
        self._move_cursor_to_end()

    def keyPressEvent(self, event):
        if self.isReadOnly():
            super().keyPressEvent(event)
            return

        if self._input_start is None:
            self.enable_prompt()

        cursor = self.textCursor()
        input_start = self._input_start
        has_ctrl = bool(event.modifiers() & QtCore.Qt.ControlModifier)
        is_navigation = event.key() in {
            QtCore.Qt.Key_Up,
            QtCore.Qt.Key_Down,
            QtCore.Qt.Key_PageUp,
            QtCore.Qt.Key_PageDown,
            QtCore.Qt.Key_End,
            QtCore.Qt.Key_Tab,
        }

        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            command = self.current_command().strip()
            if not command:
                self.enable_prompt("")
                return
            self.disable_prompt()
            self.command_submitted.emit(command)
            return
        if has_ctrl:
            super().keyPressEvent(event)
            return
        if cursor.hasSelection() and cursor.selectionStart() < input_start:
            if event.key() in (QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete) or event.text():
                self._move_cursor_to_end()
                cursor = self.textCursor()
        if event.key() == QtCore.Qt.Key_Home:
            cursor.setPosition(input_start)
            self.setTextCursor(cursor)
            return
        if is_navigation:
            super().keyPressEvent(event)
            return
        if event.key() == QtCore.Qt.Key_Backspace and cursor.position() <= input_start and not cursor.hasSelection():
            return
        if event.key() == QtCore.Qt.Key_Left and cursor.position() <= input_start and not cursor.hasSelection():
            return
        if event.key() == QtCore.Qt.Key_Delete and cursor.position() < input_start and not cursor.hasSelection():
            return
        if event.text() and cursor.position() < input_start and not cursor.hasSelection():
            self._move_cursor_to_end()

        super().keyPressEvent(event)

        if self.textCursor().position() < self._input_start and not self.textCursor().hasSelection():
            self._move_cursor_to_end()

    def _move_cursor_to_end(self):
        cursor = self.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self.setTextCursor(cursor)


class DeploymentWorker(QtCore.QObject):
    finished = QtCore.Signal(bool, str)
    log_message = QtCore.Signal(object)

    def __init__(self, config, recovery_commands=""):
        super().__init__()
        self.config = config
        self.recovery_commands = recovery_commands
        self.pause_requested = False

    @QtCore.Slot()
    def request_pause(self):
        self.pause_requested = True
        self.log_message.emit("[PAUSE] Pause requested. The deployment will stop after the current step finishes.")

    @QtCore.Slot()
    def run(self):
        success = False
        recovery = ""
        try:
            if self.recovery_commands:
                success, recovery = execute_resume_deployment(
                    self.config,
                    self.recovery_commands,
                    log_callback=self.log_message.emit,
                    should_pause=lambda: self.pause_requested,
                )
            else:
                success, recovery = execute_deployment(
                    self.config,
                    log_callback=self.log_message.emit,
                    should_pause=lambda: self.pause_requested,
                )
            if success:
                self.log_message.emit("Deployment succeeded.")
            elif self.pause_requested and recovery:
                self.log_message.emit("Deployment paused.")
            else:
                self.log_message.emit("Deployment failed.")
        except Exception as exc:
            self.log_message.emit(f"[EXCEPTION] {exc}")
            self.log_message.emit(traceback.format_exc())
            self.log_message.emit("Deployment failed due to an unexpected error.")
        finally:
            self.finished.emit(success, recovery or "")


class ManualCommandWorker(QtCore.QObject):
    finished = QtCore.Signal(int, str, str)
    log_message = QtCore.Signal(str)

    def __init__(self, config, command):
        super().__init__()
        self.config = config
        self.command = command

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        code = 1
        out = ""
        err = ""
        try:
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            command = with_configured_sudo(self.command, self.config)
            code, out, err = executor.run(command)
        except Exception as exc:
            err = f"{exc}\n{traceback.format_exc()}"
        finally:
            if executor:
                executor.close()
            self.finished.emit(code, out or "", err or "")


class SiteInventoryWorker(QtCore.QObject):
    finished = QtCore.Signal(list, str)
    log_message = QtCore.Signal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        sites = []
        error = ""
        try:
            self.log_message.emit("$ Loading sites from server...")
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            command = (
                'for root_path in "$HOME" /home /opt /var/www; do '
                '[ -d "$root_path" ] || continue; '
                'find "$root_path" -mindepth 1 -maxdepth 4 -type d -name sites 2>/dev/null; '
                'done | sort -u | while IFS= read -r sites_path; do '
                'bench_path="$(dirname "$sites_path")"; '
                '[ -d "$bench_path/apps/frappe" ] || [ -f "$sites_path/common_site_config.json" ] || continue; '
                'bench_name="$(basename "$bench_path")"; '
                'for site_path in "$sites_path"/*; do '
                '[ -d "$site_path" ] || continue; '
                'site_name="$(basename "$site_path")"; '
                '[ "$site_name" = "assets" ] && continue; '
                '[ -f "$site_path/site_config.json" ] || continue; '
                'printf "%s|%s|%s\\n" "$bench_name" "$bench_path" "$site_name"; '
                "done; "
                "done"
            )
            code, out, err = executor.run(command, check=False)
            if code != 0:
                error = err.strip() or out.strip() or "Unable to load sites from the server."
            else:
                seen = set()
                for line in out.splitlines():
                    line = line.strip()
                    if "|" not in line:
                        continue
                    parts = line.split("|", 2)
                    if len(parts) == 2:
                        bench_name, site_name = parts
                        bench_path = f"$HOME/{bench_name.strip()}"
                    else:
                        bench_name, bench_path, site_name = parts
                    key = (bench_path.strip(), site_name.strip())
                    if key in seen:
                        continue
                    seen.add(key)
                    sites.append({"bench": bench_name.strip(), "bench_path": bench_path.strip(), "site": site_name.strip()})
        except Exception as exc:
            error = str(exc)
        finally:
            if executor:
                executor.close()
            self.finished.emit(sites, error)


class SiteBackupWorker(QtCore.QObject):
    finished = QtCore.Signal(list, str)
    log_message = QtCore.Signal(str)

    def __init__(self, config, sites):
        super().__init__()
        self.config = config
        self.sites = sites

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        manifest = []
        error = ""
        try:
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            for item in self.sites:
                bench_name = item["bench"]
                bench_path = item.get("bench_path") or f"$HOME/{bench_name}"
                site_name = item["site"]
                bench_path_q = shell_quote(bench_path)
                site_name_q = shell_quote(site_name)
                self.log_message.emit(f"$ Creating backup for {site_name} in {bench_name}...")
                backup_command = (
                    f"cd {bench_path_q} && bench --site {site_name_q} backup --with-files "
                    f"&& find {shell_quote(posixpath.join(bench_path, 'sites', site_name, 'private', 'backups'))} "
                    f"{shell_quote(posixpath.join(bench_path, 'sites', site_name, 'public', 'backups'))} -type f -printf \"%T@|%p\\n\" 2>/dev/null "
                    f"| sort -t'|' -nr -k1,1 | head -n 6 | cut -d'|' -f2-"
                )
                code, out, err = executor.run(with_configured_sudo(backup_command, self.config), check=False)
                if code != 0:
                    error = err.strip() or out.strip() or f"Backup failed for {site_name}."
                    break
                files = [line.strip() for line in out.splitlines() if line.strip().startswith("/")]
                manifest.append({"bench": bench_name, "bench_path": bench_path, "site": site_name, "files": files})
                self.log_message.emit(f"[DONE] Backup ready for {site_name}")
        except Exception as exc:
            error = str(exc)
        finally:
            if executor:
                executor.close()
            self.finished.emit(manifest, error)


class SiteActionWorker(QtCore.QObject):
    finished = QtCore.Signal(str)
    log_message = QtCore.Signal(str)

    def __init__(self, config, sites, action):
        super().__init__()
        self.config = config
        self.sites = sites
        self.action = action

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        error = ""
        try:
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            for item in self.sites:
                bench_name = item["bench"]
                bench_path = item.get("bench_path") or f"$HOME/{bench_name}"
                site_name = item["site"]
                domain = item.get("domain") or site_name
                bench_path_q = shell_quote(bench_path)
                site_name_q = shell_quote(site_name)
                domain_q = shell_quote(domain)

                if self.action == "migrate":
                    self.log_message.emit(f"$ Migrating {site_name} in {bench_name}...")
                    command = f"cd {bench_path_q} && bench --site {site_name_q} migrate"
                elif self.action == "ssl":
                    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
                    key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"
                    cert_path_q = shell_quote(cert_path)
                    key_path_q = shell_quote(key_path)
                    email_q = shell_quote(f"admin@{domain}")
                    self.log_message.emit(f"$ Generating SSL for {site_name} ({domain}) in {bench_name}...")
                    command = (
                        "sudo apt-get install -y certbot python3-certbot-nginx && "
                        f"if [ -f {cert_path_q} ] && [ -f {key_path_q} ]; then "
                        f"echo 'SSL certificate already exists for {domain}'; "
                        f"else sudo certbot --nginx -d {domain_q} --non-interactive --agree-tos -m {email_q}; fi "
                        f"&& if [ -f {cert_path_q} ] && [ -f {key_path_q} ]; then "
                        f"cd {bench_path_q} && bench --site {site_name_q} set-config ssl_certificate {cert_path_q} "
                        f"&& bench --site {site_name_q} set-config ssl_certificate_key {key_path_q}; "
                        "else echo 'Certificate files are missing for this domain.'; exit 1; fi"
                    )
                else:
                    error = f"Unsupported site action: {self.action}"
                    break

                code, out, err = executor.run(with_configured_sudo(command, self.config), check=False)
                if code != 0:
                    error = err.strip() or out.strip() or f"{self.action.title()} failed for {site_name}."
                    break
                self.log_message.emit(f"[DONE] {self.action.title()} completed for {site_name}")
        except Exception as exc:
            error = str(exc)
        finally:
            if executor:
                executor.close()
            self.finished.emit(error)


class SiteDownloadWorker(QtCore.QObject):
    finished = QtCore.Signal(str)
    log_message = QtCore.Signal(str)

    def __init__(self, config, manifest, destination_root):
        super().__init__()
        self.config = config
        self.manifest = manifest
        self.destination_root = destination_root

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        error = ""
        try:
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            for entry in self.manifest:
                site_root = os.path.join(self.destination_root, entry["site"])
                os.makedirs(site_root, exist_ok=True)
                for remote_path in entry["files"]:
                    file_name = os.path.basename(remote_path)
                    local_path = os.path.join(site_root, file_name)
                    self.log_message.emit(f"$ Downloading {entry['site']}: {file_name}")
                    executor.download_file(remote_path, local_path)
            self.log_message.emit(f"[DONE] Backups downloaded to {self.destination_root}")
        except Exception as exc:
            error = str(exc)
        finally:
            if executor:
                executor.close()
            self.finished.emit(error)


class SiteUploadWorker(QtCore.QObject):
    finished = QtCore.Signal(str)
    log_message = QtCore.Signal(str)

    def __init__(self, config, source_root):
        super().__init__()
        self.config = config
        self.source_root = source_root

    @QtCore.Slot()
    def run(self):
        from frappe_auto_deploy.executor import Executor

        executor = None
        error = ""
        try:
            executor = Executor(
                remote=self.config.get("remote"),
                host=self.config.get("server_ip"),
                port=self.config.get("ssh_port", 22),
                username=self.config.get("ssh_user"),
                password=self.config.get("ssh_password"),
                use_wsl=self.config.get("use_wsl", False),
                use_ssh_key=self.config.get("use_ssh_key", False),
                ssh_private_key=self.config.get("ssh_private_key", ""),
                ssh_key_passphrase=self.config.get("ssh_key_passphrase", ""),
            )
            upload_user = (
                self.config.get("frappe_user")
                or self.config.get("new_username")
                or self.config.get("ssh_user")
                or "root"
            ).strip() or "root"
            remote_root = f"/root/frappe-backups" if upload_user == "root" else f"/home/{upload_user}/frappe-backups"
            remote_root_q = shell_quote(remote_root)
            if upload_user == "root":
                prepare_command = f"install -d -m 700 {remote_root_q}"
            else:
                upload_user_q = shell_quote(upload_user)
                prepare_command = (
                    f"install -d -m 755 -o {upload_user_q} -g {upload_user_q} {remote_root_q}"
                )
            code, out, err = executor.run(with_configured_sudo(prepare_command, self.config), check=False)
            if code != 0:
                error = err.strip() or out.strip() or f"Could not prepare upload folder {remote_root}."
                return

            file_count = 0
            for root, _, files in os.walk(self.source_root):
                for file_name in files:
                    local_path = os.path.join(root, file_name)
                    relative_path = os.path.relpath(local_path, self.source_root)
                    remote_path = posixpath.join(
                        remote_root,
                        relative_path.replace(os.sep, "/"),
                    )
                    self.log_message.emit(f"$ Uploading {relative_path} -> {remote_path}")
                    executor.upload_file(local_path, remote_path)
                    file_count += 1

            if file_count == 0:
                error = "Selected backup folder is empty."
                return

            if upload_user != "root":
                chown_command = f"chown -R {shell_quote(upload_user)}:{shell_quote(upload_user)} {remote_root_q}"
                executor.run(with_configured_sudo(chown_command, self.config), check=False)
            self.log_message.emit(f"[DONE] Uploaded {file_count} file(s) to {remote_root}")
        except Exception as exc:
            error = str(exc)
        finally:
            if executor:
                executor.close()
            self.finished.emit(error)


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.worker = None
        self.command_thread = None
        self.command_worker = None
        self.site_thread = None
        self.site_worker = None
        self.backup_thread = None
        self.backup_worker = None
        self.site_action_thread = None
        self.site_action_worker = None
        self.download_thread = None
        self.download_worker = None
        self.upload_thread = None
        self.upload_worker = None
        self.last_recovery_commands = ""
        self.deployment_pause_pending = False
        self.available_sites = []
        self.latest_backup_manifest = []
        self.is_windows_host = platform.system().lower().startswith("win")
        self.is_linux_host = platform.system().lower() == "linux"
        self.local_linux_name = self._detect_local_linux_name()
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.settings_path = os.path.join(self.project_root, "server_profile.json")
        self.step_items = {}
        self.total_steps = 0
        self.completed_steps = 0
        self.failed_steps = 0
        self.current_step_title = "idle"
        self.manual_terminal_cwd = None

        self.setWindowTitle("Frappe Auto Deploy")
        self.resize(1280, 840)
        self.setMinimumSize(960, 700)
        self.setStyleSheet(
            """
            QWidget {
                background: #eef3f6;
                color: #17313b;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QLabel#heroTitle {
                font-size: 28px;
                font-weight: 700;
                color: #113240;
            }
            QLabel#heroBody {
                color: #4f6972;
                font-size: 14px;
            }
            QFrame#topNavBar {
                background: #ffffff;
                border: 1px solid #d2dde2;
                border-radius: 20px;
            }
            QLabel#navBrand {
                color: #113240;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#navMeta {
                color: #57707a;
                font-size: 12px;
            }
            QFrame#navMenuCard {
                background: #f7fafb;
                border: 1px solid #d7e2e6;
                border-radius: 18px;
            }
            QPushButton#navMenuButton {
                background: #f3f7f9;
                color: #35515b;
                border: 1px solid #d7e2e6;
                border-radius: 14px;
                padding: 12px 18px;
                min-width: 150px;
                font-weight: 700;
                text-align: left;
            }
            QPushButton#navMenuButton:checked {
                background: #dceff1;
                color: #17313b;
                border: 1px solid #8fb5bb;
            }
            QPushButton#navMenuButton:hover {
                background: #e9f1f4;
                color: #17313b;
            }
            QFrame#heroCard {
                background: #ffffff;
                border: 1px solid #d7e2e6;
                border-radius: 20px;
            }
            QFrame#panelCard, QGroupBox, QProgressBar {
                background: #ffffff;
                border: 1px solid #d2dde2;
                border-radius: 18px;
            }
            QFrame#terminalCard {
                background: #000000;
                border: 1px solid #f0f0f0;
                border-radius: 18px;
            }
            QPlainTextEdit#terminalConsole {
                background: #000000;
                color: #ffffff;
                border: 1px solid #f0f0f0;
                border-radius: 14px;
                padding: 12px;
                font-family: Consolas, "Courier New";
                selection-background-color: #ffffff;
                selection-color: #000000;
            }
            QPlainTextEdit#processTerminal {
                background: #000000;
                color: #ffffff;
                border: 1px solid #f0f0f0;
                padding: 8px 4px 4px 4px;
                font-family: Consolas, "Courier New";
                selection-background-color: #ffffff;
                selection-color: #000000;
            }
            QGroupBox {
                font-weight: 700;
                margin-top: 10px;
                padding: 16px 14px 14px 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                top: -2px;
                padding: 0 6px;
                color: #1b4d5c;
            }
            QLineEdit, QSpinBox, QComboBox, QTextEdit {
                background: #fbfcfd;
                border: 1px solid #c8d8de;
                border-radius: 10px;
                padding: 10px 12px;
                min-height: 22px;
            }
            QAbstractScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: #edf3f5;
                width: 10px;
                border-radius: 5px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #b8cbd1;
                border-radius: 5px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {
                border: 1px solid #2f9e7c;
            }
            QCheckBox, QRadioButton {
                spacing: 8px;
                color: #1c4855;
                font-weight: 600;
                background: transparent;
            }
            QCheckBox:checked, QRadioButton:checked {
                color: #176653;
            }
            QCheckBox::indicator, QRadioButton::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator {
                border: 1px solid #91adb5;
                border-radius: 4px;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #1ea672;
                border: 1px solid #1ea672;
            }
            QRadioButton::indicator {
                border: 1px solid #91adb5;
                border-radius: 8px;
                background: #ffffff;
            }
            QRadioButton::indicator:checked {
                background: #1ea672;
                border: 4px solid #ffffff;
            }
            QPushButton {
                background: #1d7a64;
                color: white;
                border: none;
                border-radius: 10px;
                padding: 10px 16px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #176653;
            }
            QPushButton:disabled {
                background: #9fb5b7;
                color: #eef3f4;
            }
            QPushButton#secondaryButton {
                background: #e8f0f2;
                color: #1c4855;
                border: 1px solid #c8d8de;
            }
            QPushButton#secondaryButton:hover {
                background: #dce8eb;
            }
            QProgressBar {
                min-height: 16px;
                text-align: center;
                color: #17313b;
            }
            QProgressBar::chunk {
                background: #2f9e7c;
                border-radius: 10px;
            }
            QLabel#statusChip {
                background: #edf7f4;
                color: #176653;
                border: 1px solid #cbe7de;
                border-radius: 999px;
                padding: 6px 10px;
                font-weight: 700;
            }
            QLabel#hintText {
                color: #57707a;
                font-size: 12px;
            }
            QLabel#sectionTitle {
                color: #17313b;
                font-size: 15px;
                font-weight: 700;
            }
            QLabel#terminalTitle {
                color: #ffffff;
                font-size: 14px;
                font-weight: 700;
            }
            QLabel#terminalMeta {
                color: #d4d4d4;
                font-size: 11px;
            }
            QLabel#assistantTitle {
                color: #113240;
                font-size: 14px;
                font-weight: 700;
            }
            QLabel#assistantMeta {
                color: #57707a;
                font-size: 11px;
            }
            QLabel#metricValue {
                color: #113240;
                font-size: 28px;
                font-weight: 700;
            }
            QLabel#metricLabel {
                color: #57707a;
                font-size: 12px;
            }
            QListWidget#processList {
                background: #f8fbfb;
                border: 1px solid #d7e2e6;
                border-radius: 12px;
                padding: 6px;
                min-height: 150px;
            }
            QListWidget#processList::item {
                border-radius: 10px;
                padding: 8px;
                margin: 2px 0;
            }
            QLineEdit#commandInput {
                background: #000000;
                color: #ffffff;
                border: 1px solid #f0f0f0;
                border-radius: 10px;
                padding: 10px 12px;
                font-family: Consolas, "Courier New";
            }
            QLineEdit#commandInput:focus {
                border: 1px solid #ffffff;
            }
            QPushButton#terminalButton {
                background: #111111;
                color: #ffffff;
                border: 1px solid #f0f0f0;
                border-radius: 10px;
                padding: 10px 14px;
                font-weight: 700;
            }
            QPushButton#terminalButton:hover {
                background: #2a2a2a;
            }
            QTextEdit#assistantOutput {
                background: #fbfcfd;
                border: 1px solid #c8d8de;
                border-radius: 12px;
                padding: 10px 12px;
                color: #17313b;
            }
            """
        )

        self._build_ui()
        self._connect_signals()
        self.update_access_mode()
        self.update_remote_fields()
        self.update_user_fields()
        self._reset_runtime_state()
        self.load_server_profile()

    def _build_ui(self):
        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(14)

        nav_bar = QtWidgets.QFrame()
        nav_bar.setObjectName("topNavBar")
        nav_bar_layout = QtWidgets.QHBoxLayout(nav_bar)
        nav_bar_layout.setContentsMargins(18, 14, 18, 14)
        nav_bar_layout.setSpacing(14)
        nav_text_layout = QtWidgets.QVBoxLayout()
        nav_text_layout.setSpacing(2)
        nav_brand = QtWidgets.QLabel("Frappe Auto Deploy")
        nav_brand.setObjectName("navBrand")
        nav_meta = QtWidgets.QLabel("Clean tabbed workflow for server setup, site config, deploy and recovery.")
        nav_meta.setObjectName("navMeta")
        nav_meta.setWordWrap(True)
        nav_text_layout.addWidget(nav_brand)
        nav_text_layout.addWidget(nav_meta)
        nav_bar_layout.addLayout(nav_text_layout, 1)
        root_layout.addWidget(nav_bar)

        self.platform_hint = QtWidgets.QLabel(self._platform_hint_text())
        self.platform_hint.setObjectName("hintText")
        self.platform_hint.setWordWrap(True)
        root_layout.addWidget(self.platform_hint)

        nav_menu_card = QtWidgets.QFrame()
        nav_menu_card.setObjectName("navMenuCard")
        nav_menu_layout = QtWidgets.QHBoxLayout(nav_menu_card)
        nav_menu_layout.setContentsMargins(12, 12, 12, 12)
        nav_menu_layout.setSpacing(10)
        root_layout.addWidget(nav_menu_card)

        self.section_buttons = QtWidgets.QButtonGroup(self)
        self.section_buttons.setExclusive(True)
        self.server_section_button = QtWidgets.QPushButton("1. Server Details")
        self.server_section_button.setObjectName("navMenuButton")
        self.server_section_button.setCheckable(True)
        self.mysql_section_button = QtWidgets.QPushButton("2. MySQL")
        self.mysql_section_button.setObjectName("navMenuButton")
        self.mysql_section_button.setCheckable(True)
        self.bench_section_button = QtWidgets.QPushButton("3. Frappe Bench")
        self.bench_section_button.setObjectName("navMenuButton")
        self.bench_section_button.setCheckable(True)
        self.jobs_section_button = QtWidgets.QPushButton("4. Jobs")
        self.jobs_section_button.setObjectName("navMenuButton")
        self.jobs_section_button.setCheckable(True)
        self.troubleshoot_section_button = QtWidgets.QPushButton("5. Troubleshoot")
        self.troubleshoot_section_button.setObjectName("navMenuButton")
        self.troubleshoot_section_button.setCheckable(True)
        for index, button in enumerate(
            [
                self.server_section_button,
                self.mysql_section_button,
                self.bench_section_button,
                self.jobs_section_button,
                self.troubleshoot_section_button,
            ]
        ):
            self.section_buttons.addButton(button, index)
            nav_menu_layout.addWidget(button)
        nav_menu_layout.addStretch(1)

        self.server_tab = QtWidgets.QWidget()
        self.mysql_tab = QtWidgets.QWidget()
        self.bench_tab = QtWidgets.QWidget()
        self.jobs_tab = QtWidgets.QWidget()
        self.troubleshoot_tab = QtWidgets.QWidget()
        self.sections_stack = QtWidgets.QStackedWidget()
        self.sections_stack.addWidget(self.server_tab)
        self.sections_stack.addWidget(self.mysql_tab)
        self.sections_stack.addWidget(self.bench_tab)
        self.sections_stack.addWidget(self.jobs_tab)
        self.sections_stack.addWidget(self.troubleshoot_tab)
        root_layout.addWidget(self.sections_stack, 1)

        server_scroll = QtWidgets.QScrollArea()
        server_scroll.setWidgetResizable(True)
        server_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        server_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        server_root = QtWidgets.QVBoxLayout(self.server_tab)
        server_root.setContentsMargins(0, 0, 0, 0)
        server_root.addWidget(server_scroll)
        server_content = QtWidgets.QWidget()
        server_layout = QtWidgets.QVBoxLayout(server_content)
        server_layout.setContentsMargins(0, 0, 0, 0)
        server_layout.setSpacing(14)
        server_scroll.setWidget(server_content)

        self.remote_group = QtWidgets.QGroupBox("Deployment Location")
        remote_layout = QtWidgets.QVBoxLayout(self.remote_group)
        remote_row = QtWidgets.QHBoxLayout()
        self.same_radio = QtWidgets.QRadioButton(self._same_system_label_text())
        self.remote_radio = QtWidgets.QRadioButton("Remote Server")
        self.same_radio.setChecked(True)
        self.remote_radio.setChecked(False)
        remote_row.addWidget(self.same_radio)
        remote_row.addWidget(self.remote_radio)
        remote_row.addStretch(1)
        self.location_note = QtWidgets.QLabel()
        self.location_note.setObjectName("hintText")
        self.location_note.setWordWrap(True)
        remote_layout.addLayout(remote_row)
        remote_layout.addWidget(self.location_note)
        server_layout.addWidget(self.remote_group)

        self.server_group = QtWidgets.QGroupBox("Server Connection")
        server_form = QtWidgets.QFormLayout(self.server_group)
        self._configure_form_layout(server_form)
        access_row = QtWidgets.QHBoxLayout()
        self.root_radio = QtWidgets.QRadioButton("Root")
        self.user_radio = QtWidgets.QRadioButton("User")
        self.root_radio.setChecked(True)
        access_row.addWidget(self.root_radio)
        access_row.addWidget(self.user_radio)
        access_row.addStretch(1)
        self.server_ip = QtWidgets.QLineEdit()
        self.server_ip.setPlaceholderText("e.g. 192.168.1.100")
        self.ssh_port = QtWidgets.QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        self.ssh_user = QtWidgets.QLineEdit()
        self.ssh_password = QtWidgets.QLineEdit()
        self.ssh_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ssh_password.setPlaceholderText("Required for remote login")
        self.use_ssh_key_checkbox = QtWidgets.QCheckBox("Use SSH private key for connection")
        self.ssh_private_key = QtWidgets.QPlainTextEdit()
        self.ssh_private_key.setPlaceholderText("Paste private key used for login, e.g. -----BEGIN OPENSSH PRIVATE KEY-----")
        self.ssh_private_key.setFixedHeight(104)
        self.ssh_key_passphrase = QtWidgets.QLineEdit()
        self.ssh_key_passphrase.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ssh_key_passphrase.setPlaceholderText("Only if your private key has a passphrase")
        self.ssh_public_key = QtWidgets.QPlainTextEdit()
        self.ssh_public_key.setPlaceholderText("Optional: paste public key to install on the server, e.g. ssh-ed25519 AAAA...")
        self.ssh_public_key.setFixedHeight(78)
        self.sudo_password = QtWidgets.QLineEdit()
        self.sudo_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.sudo_password.setPlaceholderText("Needed for non-root sudo flows")
        server_form.addRow("Access Mode", self._wrap_layout(access_row))
        server_form.addRow("Server IP", self.server_ip)
        server_form.addRow("SSH Port", self.ssh_port)
        server_form.addRow("SSH User", self.ssh_user)
        server_form.addRow("SSH Password", self.ssh_password)
        server_form.addRow("", self.use_ssh_key_checkbox)
        server_form.addRow("SSH Private Key", self.ssh_private_key)
        server_form.addRow("Key Passphrase", self.ssh_key_passphrase)
        server_form.addRow("SSH Public Key", self.ssh_public_key)
        server_form.addRow("Sudo Password", self.sudo_password)
        server_layout.addWidget(self.server_group)

        self.user_group = QtWidgets.QGroupBox("Optional User Creation")
        user_form = QtWidgets.QFormLayout(self.user_group)
        self._configure_form_layout(user_form)
        self.create_user_checkbox = QtWidgets.QCheckBox("Create a new Linux user before setup")
        self.username = QtWidgets.QLineEdit()
        self.username.setPlaceholderText("Username")
        self.password = QtWidgets.QLineEdit()
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        user_form.addRow(self.create_user_checkbox)
        user_form.addRow("Username", self.username)
        user_form.addRow("Password", self.password)
        server_layout.addWidget(self.user_group)

        self.swap_group = QtWidgets.QGroupBox("Swap Memory")
        swap_form = QtWidgets.QFormLayout(self.swap_group)
        self._configure_form_layout(swap_form)
        self.create_swap_checkbox = QtWidgets.QCheckBox("Create swap before all other deployment steps")
        self.create_swap_checkbox.setChecked(False)
        self.swap_size_gb = QtWidgets.QSpinBox()
        self.swap_size_gb.setRange(1, 128)
        self.swap_size_gb.setValue(4)
        swap_form.addRow(self.create_swap_checkbox)
        swap_form.addRow("Swap Size (GB)", self.swap_size_gb)
        server_layout.addWidget(self.swap_group)

        server_actions = QtWidgets.QHBoxLayout()
        server_actions.addStretch(1)
        self.save_server_button = QtWidgets.QPushButton("Save Server Info")
        self.save_server_button.setObjectName("secondaryButton")
        self.load_server_button = QtWidgets.QPushButton("Load Saved Info")
        self.load_server_button.setObjectName("secondaryButton")
        server_actions.addWidget(self.load_server_button)
        server_actions.addWidget(self.save_server_button)
        server_layout.addLayout(server_actions)
        server_layout.addStretch(1)

        bench_scroll = QtWidgets.QScrollArea()
        bench_scroll.setWidgetResizable(True)
        bench_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        bench_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        bench_root = QtWidgets.QVBoxLayout(self.bench_tab)
        bench_root.setContentsMargins(0, 0, 0, 0)
        bench_root.addWidget(bench_scroll)
        bench_content = QtWidgets.QWidget()
        bench_layout = QtWidgets.QVBoxLayout(bench_content)
        bench_layout.setContentsMargins(0, 0, 0, 0)
        bench_layout.setSpacing(14)
        bench_scroll.setWidget(bench_content)

        self.erpnext_group = QtWidgets.QGroupBox("Bench, Site & Domain")
        erp_form = QtWidgets.QFormLayout(self.erpnext_group)
        self._configure_form_layout(erp_form)
        self.bench_name_input = QtWidgets.QLineEdit()
        self.bench_name_input.setPlaceholderText("e.g. bench-15 or my-bench")
        self.site_name = QtWidgets.QLineEdit()
        self.site_name.setPlaceholderText("e.g. example.local")
        self.create_site_checkbox = QtWidgets.QCheckBox("Create site during deployment")
        self.create_site_checkbox.setChecked(False)
        self.site_admin_password = QtWidgets.QLineEdit()
        self.site_admin_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.site_admin_password.setPlaceholderText("Defaults to admin")
        self.mariadb_root_password = QtWidgets.QLineEdit()
        self.mariadb_root_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.mariadb_root_password.setPlaceholderText("Defaults to root")
        self.domain_name = QtWidgets.QLineEdit()
        self.domain_name.setPlaceholderText("Only needed for SSL")
        self.bench_branch = QtWidgets.QComboBox()
        self.bench_branch.addItems(["version-15", "version-14", "version-13", "version-16"])
        self.use_nvm_checkbox = QtWidgets.QCheckBox("Manage Node.js with NVM")
        self.use_nvm_checkbox.setChecked(True)
        self.setup_production_checkbox = QtWidgets.QCheckBox("Run production setup")
        self.setup_production_checkbox.setChecked(False)
        self.generate_ssl_checkbox = QtWidgets.QCheckBox("Generate SSL with Certbot")
        self.generate_ssl_checkbox.setChecked(False)
        erp_form.addRow("Bench Name", self.bench_name_input)
        erp_form.addRow("Bench Version", self.bench_branch)
        erp_form.addRow(self.create_site_checkbox)
        erp_form.addRow("Site Name", self.site_name)
        erp_form.addRow("Admin Password", self.site_admin_password)
        erp_form.addRow("Domain", self.domain_name)
        erp_form.addRow(self.use_nvm_checkbox)
        erp_form.addRow(self.setup_production_checkbox)
        erp_form.addRow(self.generate_ssl_checkbox)
        bench_layout.addWidget(self.erpnext_group)
        deploy_layout = QtWidgets.QVBoxLayout()
        deploy_layout.setSpacing(14)
        self.status_chip = QtWidgets.QLabel("Ready")
        self.status_chip.setObjectName("statusChip")
        self.active_step_label = QtWidgets.QLabel("Current step: idle")
        self.process_overview_label = QtWidgets.QLabel("Process overview: waiting for deployment plan")
        self.process_overview_label.setObjectName("hintText")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.recovery_button = QtWidgets.QPushButton("Copy Recovery Commands")
        self.recovery_button.setObjectName("secondaryButton")
        self.recovery_button.setEnabled(False)
        self.run_button = QtWidgets.QPushButton("Start Deployment")
        self.summary_card = QtWidgets.QFrame()
        self.summary_card.setObjectName("panelCard")
        summary_layout = QtWidgets.QGridLayout(self.summary_card)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setHorizontalSpacing(18)
        summary_layout.setVerticalSpacing(10)
        summary_title = QtWidgets.QLabel("Live Deployment Summary")
        summary_title.setObjectName("sectionTitle")
        self.total_steps_value = QtWidgets.QLabel("0")
        self.total_steps_value.setObjectName("metricValue")
        self.completed_steps_value = QtWidgets.QLabel("0")
        self.completed_steps_value.setObjectName("metricValue")
        self.failed_steps_value = QtWidgets.QLabel("0")
        self.failed_steps_value.setObjectName("metricValue")
        self.result_value = QtWidgets.QLabel("Waiting")
        self.result_value.setObjectName("metricValue")
        summary_layout.addWidget(summary_title, 0, 0, 1, 4)
        summary_layout.addWidget(self.total_steps_value, 1, 0)
        summary_layout.addWidget(self.completed_steps_value, 1, 1)
        summary_layout.addWidget(self.failed_steps_value, 1, 2)
        summary_layout.addWidget(self.result_value, 1, 3)
        summary_layout.addWidget(self._metric_label("Total Steps"), 2, 0)
        summary_layout.addWidget(self._metric_label("Completed"), 2, 1)
        summary_layout.addWidget(self._metric_label("Failed"), 2, 2)
        summary_layout.addWidget(self._metric_label("Result"), 2, 3)
        deploy_help = QtWidgets.QLabel(
            "Flow: 1) Save server info  2) Fill bench/site data  3) Start deployment  4) Check process tab for live output."
        )
        deploy_help.setObjectName("hintText")
        deploy_help.setWordWrap(True)
        deploy_layout.addWidget(self.status_chip, 0, QtCore.Qt.AlignLeft)
        deploy_layout.addWidget(self.active_step_label)
        deploy_layout.addWidget(self.progress_bar)
        deploy_layout.addWidget(self.summary_card)
        deploy_layout.addWidget(deploy_help)
        deploy_actions = QtWidgets.QHBoxLayout()
        deploy_actions.addWidget(self.recovery_button)
        deploy_actions.addWidget(self.run_button)
        deploy_layout.addLayout(deploy_actions)
        bench_layout.addLayout(deploy_layout)
        deploy_layout.addStretch(1)

        jobs_layout_root = QtWidgets.QHBoxLayout(self.jobs_tab)
        jobs_layout_root.setContentsMargins(0, 0, 0, 0)
        jobs_layout_root.setSpacing(12)
        self.process_card = QtWidgets.QFrame()
        self.process_card.setObjectName("panelCard")
        process_layout = QtWidgets.QVBoxLayout(self.process_card)
        process_layout.setContentsMargins(16, 14, 16, 14)
        process_layout.setSpacing(10)
        process_title = QtWidgets.QLabel("Process Tracker")
        process_title.setObjectName("sectionTitle")
        self.process_list = QtWidgets.QListWidget()
        self.process_list.setObjectName("processList")
        process_layout.addWidget(process_title)
        process_layout.addWidget(self.process_overview_label)
        process_layout.addWidget(self.process_list)
        jobs_layout_root.addWidget(self.process_card, 1)

        jobs_right_panel = QtWidgets.QVBoxLayout()
        jobs_right_panel.setSpacing(12)
        self.process_terminal_card = QtWidgets.QFrame()
        self.process_terminal_card.setObjectName("terminalCard")
        process_terminal_layout = QtWidgets.QVBoxLayout(self.process_terminal_card)
        process_terminal_layout.setContentsMargins(16, 14, 16, 14)
        process_terminal_layout.setSpacing(10)
        self.process_terminal = TerminalConsole()
        self.process_terminal.setObjectName("processTerminal")
        self.process_terminal.setPlaceholderText("Click here and type a command. Press Enter to run. Use /resume, /pause, or /recovery for deployment shortcuts.")
        self.process_terminal.document().setMaximumBlockCount(4000)
        process_terminal_layout.addWidget(self.process_terminal, 1)
        jobs_right_panel.addWidget(self.process_terminal_card, 2)
        self.log_console = self.process_terminal
        self.process_terminal.set_prompt(self._terminal_prompt_text())
        self.process_terminal.enable_prompt()
        jobs_layout_root.addLayout(jobs_right_panel, 2)

        troubleshoot_layout = QtWidgets.QVBoxLayout(self.troubleshoot_tab)
        troubleshoot_layout.setContentsMargins(0, 0, 0, 0)
        troubleshoot_layout.setSpacing(12)
        self.backup_card = QtWidgets.QFrame()
        self.backup_card.setObjectName("panelCard")
        backup_layout = QtWidgets.QVBoxLayout(self.backup_card)
        backup_layout.setContentsMargins(16, 14, 16, 14)
        backup_layout.setSpacing(10)
        self.site_list_title = QtWidgets.QLabel("Site Backups")
        self.site_list_title.setObjectName("sectionTitle")
        backup_hint = QtWidgets.QLabel("Load all sites from the server, tick the ones you need, then create, download, or upload backups.")
        backup_hint.setObjectName("hintText")
        backup_hint.setWordWrap(True)
        self.site_list = QtWidgets.QListWidget()
        self.site_list.setObjectName("processList")
        self.site_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.site_list.setMinimumHeight(260)
        site_actions = QtWidgets.QHBoxLayout()
        site_actions.setSpacing(8)
        self.load_sites_button = QtWidgets.QPushButton("Load Sites")
        self.load_sites_button.setObjectName("secondaryButton")
        self.migrate_site_button = QtWidgets.QPushButton("Migrate")
        self.migrate_site_button.setObjectName("secondaryButton")
        self.migrate_site_button.setEnabled(False)
        self.generate_site_ssl_button = QtWidgets.QPushButton("Generate SSL")
        self.generate_site_ssl_button.setObjectName("secondaryButton")
        self.generate_site_ssl_button.setEnabled(False)
        self.take_backup_button = QtWidgets.QPushButton("Take Backup")
        self.take_backup_button.setObjectName("secondaryButton")
        self.take_backup_button.setEnabled(False)
        self.download_backup_button = QtWidgets.QPushButton("Download Backups")
        self.download_backup_button.setObjectName("secondaryButton")
        self.download_backup_button.setEnabled(False)
        self.upload_backup_button = QtWidgets.QPushButton("Upload Backup Folder")
        self.upload_backup_button.setObjectName("secondaryButton")
        site_actions.addWidget(self.load_sites_button)
        site_actions.addWidget(self.migrate_site_button)
        site_actions.addWidget(self.generate_site_ssl_button)
        site_actions.addWidget(self.take_backup_button)
        site_actions.addWidget(self.download_backup_button)
        site_actions.addWidget(self.upload_backup_button)
        backup_layout.addWidget(self.site_list_title)
        backup_layout.addWidget(backup_hint)
        backup_layout.addWidget(self.site_list, 1)
        backup_layout.addLayout(site_actions)
        troubleshoot_layout.addWidget(self.backup_card, 1)

        mysql_root = QtWidgets.QVBoxLayout(self.mysql_tab)
        mysql_root.setContentsMargins(0, 0, 0, 0)
        mysql_root.setSpacing(12)
        self.mysql_group = QtWidgets.QGroupBox("MySQL / MariaDB Options")
        mysql_form = QtWidgets.QFormLayout(self.mysql_group)
        self._configure_form_layout(mysql_form)
        mysql_mode_row = QtWidgets.QVBoxLayout()
        mysql_mode_row.setSpacing(8)
        self.mysql_keep_radio = QtWidgets.QRadioButton("Use installed MariaDB and keep the existing setup")
        self.mysql_reset_radio = QtWidgets.QRadioButton("Use installed MariaDB and reset the root password")
        self.mysql_reinstall_radio = QtWidgets.QRadioButton("Fresh install MariaDB by removing old packages and data first")
        self.mysql_reset_radio.setChecked(True)
        mysql_mode_row.addWidget(self.mysql_keep_radio)
        mysql_mode_row.addWidget(self.mysql_reset_radio)
        mysql_mode_row.addWidget(self.mysql_reinstall_radio)
        self.current_mariadb_root_password = QtWidgets.QLineEdit()
        self.current_mariadb_root_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.current_mariadb_root_password.setPlaceholderText("Optional if local root auth still works")
        self.mariadb_root_password_hint = QtWidgets.QLabel(
            "If you forgot the current MariaDB root password, choose the reinstall option and set a new password below."
        )
        self.mariadb_root_password_hint.setObjectName("hintText")
        self.mariadb_root_password_hint.setWordWrap(True)
        mysql_form.addRow("Action", self._wrap_layout(mysql_mode_row))
        mysql_form.addRow("Current Root Password", self.current_mariadb_root_password)
        mysql_form.addRow("New Root Password", self.mariadb_root_password)
        mysql_form.addRow(self.mariadb_root_password_hint)
        mysql_root.addWidget(self.mysql_group)
        mysql_root.addStretch(1)

        self.ai_prompt_input = QtWidgets.QLineEdit()
        self.ai_prompt_input.setText("")
        self.ai_prompt_input.hide()
        self.ai_suggestion_box = QtWidgets.QTextEdit()
        self.ai_suggestion_box.setReadOnly(True)
        self.ai_suggestion_box.hide()
        self.switch_section(0)

    def _connect_signals(self):
        self.remote_radio.toggled.connect(self.update_remote_fields)
        self.root_radio.toggled.connect(self.update_access_mode)
        self.user_radio.toggled.connect(self.update_access_mode)
        self.server_ip.textChanged.connect(self._refresh_terminal_prompt)
        self.ssh_user.textChanged.connect(self._refresh_terminal_prompt)
        self.use_ssh_key_checkbox.toggled.connect(self.update_remote_fields)
        self.create_user_checkbox.toggled.connect(self.update_user_fields)
        self.generate_ssl_checkbox.toggled.connect(self.update_ssl_state)
        self.recovery_button.clicked.connect(self.copy_recovery)
        self.run_button.clicked.connect(self.on_deploy)
        self.process_terminal.command_submitted.connect(self.run_manual_command)
        self.load_sites_button.clicked.connect(self.load_server_sites)
        self.migrate_site_button.clicked.connect(self.migrate_selected_sites)
        self.generate_site_ssl_button.clicked.connect(self.generate_ssl_for_selected_sites)
        self.take_backup_button.clicked.connect(self.take_selected_site_backups)
        self.download_backup_button.clicked.connect(self.download_site_backups)
        self.upload_backup_button.clicked.connect(self.upload_backup_folder)
        self.site_list.itemChanged.connect(self.update_backup_button_state)
        self.save_server_button.clicked.connect(self.save_server_profile)
        self.load_server_button.clicked.connect(self.load_server_profile)
        self.ai_prompt_input.returnPressed.connect(self.generate_codex_suggestion)
        self.section_buttons.idClicked.connect(self.switch_section)

    def _platform_hint_text(self):
        if self.is_windows_host:
            return "Windows detected: Same System runs inside WSL Ubuntu. If WSL is missing, setup will install it first."
        if self.is_linux_host:
            distro_name = self.local_linux_name or "Linux"
            return f"{distro_name} detected: Same System runs directly on this machine."
        return "Same System runs directly on this machine."

    def _detect_local_linux_name(self):
        if not self.is_linux_host:
            return ""
        try:
            os_release = platform.freedesktop_os_release()
            return os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Linux"
        except Exception:
            pass
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as handle:
                data = {}
                for raw_line in handle:
                    line = raw_line.strip()
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key] = value.strip().strip('"')
                return data.get("PRETTY_NAME") or data.get("NAME") or "Linux"
        except OSError:
            return "Linux"

    def _same_system_label_text(self):
        if self.is_windows_host:
            return "Same System (WSL Ubuntu)"
        if self.is_linux_host:
            distro_name = self.local_linux_name or "Local Linux"
            return f"Same System ({distro_name})"
        return "Same System"

    def _current_target_description(self):
        if self.remote_radio.isChecked():
            host = self.server_ip.text().strip() or "remote host"
            port = self.ssh_port.value()
            return f"Remote Server via SSH: {host}:{port}"
        if self.is_windows_host:
            return "Local System: WSL Ubuntu target"
        if self.is_linux_host:
            return f"Local System: {self.local_linux_name or 'Linux'}"
        return "Local System"

    def _monitor_header_text(self):
        return "Server Monitor" if self.remote_radio.isChecked() else "System Monitor"

    def _monitor_hint_text(self):
        scope = "server" if self.remote_radio.isChecked() else "system"
        return f"Live checks for the current {scope}: CPU/load, memory, disk, services, bench health, and running jobs."

    def _default_terminal_cwd(self, config=None):
        if config is None:
            config = self._manual_command_config()
        if config.get("remote"):
            user = (config.get("ssh_user") or "root").strip() or "root"
            return "/root" if user == "root" else f"/home/{user}"
        if config.get("use_wsl"):
            return "/root"
        if self.is_windows_host:
            return self.project_root
        return os.path.expanduser("~")

    def _current_terminal_cwd(self, config=None):
        return self.manual_terminal_cwd or self._default_terminal_cwd(config)

    def _cwd_label_for_prompt(self, cwd, config=None):
        if config is None:
            config = self._manual_command_config()
        if not cwd:
            return "~"
        home_dir = self._default_terminal_cwd(config)
        normalized_cwd = str(cwd).replace("\\", "/")
        normalized_home = str(home_dir).replace("\\", "/")
        if normalized_cwd == normalized_home:
            return "~"
        if normalized_cwd.startswith(normalized_home.rstrip("/") + "/"):
            return "~" + normalized_cwd[len(normalized_home):]
        return normalized_cwd

    def _clean_terminal_text(self, text):
        cleaned = ANSI_ESCAPE_RE.sub("", str(text or ""))
        cleaned = cleaned.replace("\x00", "")
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        return cleaned

    def _format_terminal_output(self, text):
        cleaned = self._clean_terminal_text(text)
        lines = [line.rstrip() for line in cleaned.split("\n")]
        return "\n".join(line for line in lines if line.strip())

    def _extract_manual_cwd(self, stdout_text="", stderr_text=""):
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part)
        matches = MANUAL_CWD_MARKER_RE.findall(combined)
        return matches[-1].strip() if matches else ""

    def _remove_manual_cwd_marker(self, text):
        if not text:
            return ""
        return MANUAL_CWD_MARKER_RE.sub("", text).strip()

    def _wrap_manual_command(self, command, config):
        if self.is_windows_host and not config.get("remote") and not config.get("use_wsl"):
            return command
        current_cwd_q = shell_quote(self._current_terminal_cwd(config))
        return (
            f"__fad_cwd={current_cwd_q}; "
            'if [ -n "$__fad_cwd" ]; then cd "$__fad_cwd" || exit $?; fi; '
            f"{command}; "
            '__fad_status=$?; '
            'printf "\\n__FAD_PWD__=%s\\n" "$PWD"; '
            'exit $__fad_status'
        )

    def _terminal_prompt_text(self, config=None):
        if config is None:
            config = {
                "remote": bool(getattr(self, "remote_radio", None) and self.remote_radio.isChecked()),
                "use_wsl": bool(self.is_windows_host and getattr(self, "same_radio", None) and self.same_radio.isChecked()),
                "ssh_user": self.ssh_user.text().strip() if hasattr(self, "ssh_user") else "",
                "server_ip": self.server_ip.text().strip() if hasattr(self, "server_ip") else "",
            }
        if config.get("remote"):
            user = (config.get("ssh_user") or "root").strip() or "root"
            host = (config.get("server_ip") or "remote-host").strip() or "remote-host"
            cwd_label = self._cwd_label_for_prompt(self._current_terminal_cwd(config), config)
            return f"{user}@{host}:{cwd_label}$ "
        if config.get("use_wsl"):
            cwd_label = self._cwd_label_for_prompt(self._current_terminal_cwd(config), config)
            return f"root@Ubuntu-22.04:{cwd_label}$ "
        user = (
            config.get("ssh_user")
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or "user"
        )
        host = platform.node() or "localhost"
        cwd_label = self._cwd_label_for_prompt(self._current_terminal_cwd(config), config)
        return f"{user}@{host}:{cwd_label}$ "

    def _refresh_terminal_prompt(self):
        if hasattr(self, "process_terminal"):
            self.process_terminal.set_prompt(self._terminal_prompt_text())

    def _monitor_command(self):
        bench_name = self._current_bench_name()
        target_label = shell_quote(self._current_target_description())
        return (
            f"echo '=== Target ===' && echo {target_label} && "
            "hostnamectl 2>/dev/null | sed -n '1,10p' || (echo 'Hostname:' && hostname && uname -a) "
            "&& echo && echo '=== Resources ===' "
            "&& echo 'Uptime and load:' && uptime "
            "&& echo && echo 'CPU cores:' && nproc "
            "&& echo && echo 'Memory and swap:' && free -h "
            "&& echo && echo 'Disk usage:' && df -h / "
            "&& echo && echo '=== Jobs ===' "
            "&& ps -eo pid,ppid,%cpu,%mem,stat,etimes,comm,args --sort=-%cpu | head -n 20 "
            "&& echo && echo '=== Services ===' "
            "&& (systemctl --no-pager --full --type=service --state=running 2>/dev/null | sed -n '1,25p' || service --status-all 2>/dev/null | tail -n 25 || true) "
            "&& echo && echo '=== Bench ===' "
            f"&& if [ -d {shell_quote(bench_name)} ]; then cd {shell_quote(bench_name)} && bench doctor; else echo 'Bench path not found: {bench_name}'; fi "
            "&& echo && echo '=== Supervisor / Queue ===' "
            "&& (supervisorctl status 2>/dev/null || echo 'Supervisor not configured') "
            "&& echo && echo '=== Network ===' "
            "&& (ss -tulpn 2>/dev/null | head -n 20 || netstat -tulpn 2>/dev/null | head -n 20 || true)"
        )

    def _refresh_monitor_labels(self):
        if hasattr(self, "same_radio"):
            self.same_radio.setText(self._same_system_label_text())
        if hasattr(self, "monitor_title"):
            self.monitor_title.setText(self._monitor_header_text())
        if hasattr(self, "monitor_hint_label"):
            self.monitor_hint_label.setText(self._monitor_hint_text())
        if hasattr(self, "monitor_target_value"):
            self.monitor_target_value.setText(self._current_target_description())

    def _wrap_layout(self, layout):
        container = QtWidgets.QWidget()
        container.setLayout(layout)
        return container

    def _configure_form_layout(self, layout):
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)
        layout.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        layout.setFormAlignment(QtCore.Qt.AlignTop)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        layout.setRowWrapPolicy(QtWidgets.QFormLayout.DontWrapRows)

    def _metric_label(self, text):
        label = QtWidgets.QLabel(text)
        label.setObjectName("metricLabel")
        return label

    def switch_section(self, index):
        if 0 <= index < self.sections_stack.count():
            self.sections_stack.setCurrentIndex(index)
            for button in self.section_buttons.buttons():
                button.setChecked(self.section_buttons.id(button) == index)

    def _scroll_logs(self):
        scroll_bar = self.log_console.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _set_status_chip(self, text, background, foreground, border):
        self.status_chip.setText(text)
        self.status_chip.setStyleSheet(
            f"background: {background}; color: {foreground}; border: 1px solid {border}; "
            "border-radius: 999px; padding: 6px 10px; font-weight: 700;"
        )

    def _set_process_item(self, index, title, status, detail=""):
        item = self.step_items.get(index)
        if item is None:
            item = QtWidgets.QListWidgetItem()
            self.step_items[index] = item
            self.process_list.addItem(item)

        prefix_map = {
            "pending": "[PENDING]",
            "running": "[RUNNING]",
            "done": "[DONE]",
            "failed": "[FAILED]",
        }
        color_map = {
            "pending": QtGui.QColor("#46616b"),
            "running": QtGui.QColor("#1b4d5c"),
            "done": QtGui.QColor("#176653"),
            "failed": QtGui.QColor("#a63f3f"),
        }
        base_text = f"{prefix_map.get(status, '[INFO]')} {index}. {title}"
        if detail:
            base_text = f"{base_text}\n{detail}"
        item.setText(base_text)
        item.setForeground(color_map.get(status, QtGui.QColor("#17313b")))

    def _reset_runtime_state(self):
        self.step_items = {}
        self.total_steps = 0
        self.completed_steps = 0
        self.failed_steps = 0
        self.current_step_title = "idle"
        self.process_list.clear()
        self.total_steps_value.setText("0")
        self.completed_steps_value.setText("0")
        self.failed_steps_value.setText("0")
        self.result_value.setText("Waiting")
        self.process_overview_label.setText("Process overview: waiting for deployment plan")
        self.update_backup_button_state()
        self.download_backup_button.setEnabled(bool(self.latest_backup_manifest))
        self.ai_suggestion_box.setPlainText(self._build_codex_suggestion())

    def _update_summary(self):
        self.total_steps_value.setText(str(self.total_steps))
        self.completed_steps_value.setText(str(self.completed_steps))
        self.failed_steps_value.setText(str(self.failed_steps))
        self.ai_suggestion_box.setPlainText(self._build_codex_suggestion(self.ai_prompt_input.text().strip()))
        self._refresh_recommended_command()

    def _append_console_line(self, text):
        self.log_console.append_output(text)
        self._scroll_logs()
        self.ai_suggestion_box.setPlainText(self._build_codex_suggestion(self.ai_prompt_input.text().strip()))
        self._refresh_recommended_command()

    def _append_process_terminal_line(self, text):
        self.process_terminal.append_output(text)
        scroll_bar = self.process_terminal.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _latest_console_lines(self, limit=8):
        lines = [line.strip() for line in self.log_console.toPlainText().splitlines() if line.strip()]
        return lines[-limit:]

    def _suggest_command_from_logs(self):
        log_text = self.log_console.toPlainText()
        if "The engine \"node\" is incompatible" in log_text or "Expected version \">=18\". Got \"12" in log_text:
            return 'export NVM_DIR="$HOME/.nvm" && mkdir -p "$NVM_DIR" && curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash && . "$NVM_DIR/nvm.sh" && nvm install 18 && nvm use 18 && nvm alias default 18 && npm install -g yarn && node -v && npm -v && yarn -v'
        if "npm: command not found" in log_text:
            return 'export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && nvm install 18 && nvm use 18 && npm install -g yarn'
        if "No interpreter found at path" in log_text or "uv python find" in log_text:
            return "uv python list && uv python install 3.11 && uv python find 3.11"
        if "No module named '_bz2'" in log_text or "No module named '_sqlite3'" in log_text or "No module named '_lzma'" in log_text:
            return "sudo apt-get install -y libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses5-dev libncursesw5-dev zlib1g-dev tk-dev libgdbm-dev libnss3-dev uuid-dev && uv python install --reinstall 3.11"
        if "dpkg was interrupted" in log_text:
            return "sudo dpkg --configure -a"
        if "Waiting for another package process to finish" in log_text:
            return "ps aux | grep apt && sudo lsof /var/lib/dpkg/lock-frontend"
        if "Unable to connect to port" in log_text or "could not connect to" in log_text:
            return "nc -zv <server-ip> 22"
        if "ALTER USER 'root'@'localhost'" in log_text or "Access denied for user 'root'@'localhost'" in log_text:
            password = self.collect_config().get("mariadb_root_password", "root")
            return f"sudo mariadb -e \"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}'; FLUSH PRIVILEGES;\""
        if "System update" in self.current_step_title:
            return "ps aux | grep apt; sudo tail -n 120 /var/log/apt/history.log"
        return ""

    def _refresh_recommended_command(self):
        suggested = self._suggest_command_from_logs()
        if suggested:
            self.process_terminal.set_current_command(suggested)
        elif self.last_recovery_commands:
            commands = deserialize_recovery_commands(self.last_recovery_commands)
            first_recovery = commands[0].strip() if commands else ""
            if first_recovery:
                self.process_terminal.set_current_command(first_recovery)

    def _build_codex_suggestion(self, prompt_text=""):
        config = self.collect_config()
        log_text = self.log_console.toPlainText()
        tips = []
        if prompt_text:
            tips.append(f"Request: {prompt_text}")
        tips.append(f"Current flow: {self.current_step_title}")

        if "dpkg was interrupted" in log_text:
            tips.append("Suggested fix: run `sudo dpkg --configure -a` on the server, then retry the deployment.")
            tips.append("If that command reports more package issues, run `sudo apt-get install -f` and then start again.")
        elif "Unable to connect to port" in log_text or "could not connect to" in log_text:
            tips.append("Suggested fix: verify the server IP, SSH port, and firewall rules first.")
            tips.append("Manual checks: `nc -zv <server-ip> 22` from your machine, and confirm SSH is running on the server.")
        elif "Waiting for another package process to finish" in log_text:
            tips.append("Suggested fix: another apt or dpkg process is holding the package manager lock.")
            tips.append("Manual checks: `ps aux | grep apt` and `sudo lsof /var/lib/dpkg/lock-frontend`.")
        elif "ERROR 1064" in log_text and "ALTER USER 'root'@'localhost'" in log_text:
            password = config.get("mariadb_root_password", "root")
            tips.append("Suggested fix: connect to MariaDB and set the root password with a version-compatible command.")
            tips.append(
                "Run: "
                f"`sudo mariadb -e \"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}'; FLUSH PRIVILEGES;\"`"
            )
            tips.append("If that still fails, run `sudo mariadb` first and check the exact MariaDB version with `SELECT VERSION();`.")
        elif "View 'mysql.user' references invalid table" in log_text:
            tips.append("Suggested fix: do not update `mysql.user` directly on this MariaDB version.")
            tips.append("Use `ALTER USER 'root'@'localhost' IDENTIFIED BY 'your_password';` instead.")
        elif "Network issue detected on the server" in log_text:
            tips.append("Suggested fix: repair outbound internet or DNS on the server before retrying.")
            tips.append("Manual checks: `ping -c 1 8.8.8.8`, `getent hosts github.com`, and `curl -I https://github.com`.")

        if config["remote"] and not config["server_ip"]:
            tips.append("Remote mode is enabled, but the server IP is missing.")
        if config["remote"] and config["access_mode"] == "user" and not config["ssh_user"]:
            tips.append("User access is selected, but the SSH username is missing.")
        if config["remote"] and int(config.get("ssh_port", 22)) == 1:
            tips.append("SSH port is set to `1`. This is usually incorrect. Use `22` unless your server uses a custom SSH port.")
        if not config["site_name"]:
            tips.append("Site Name is required before deployment can continue.")
        if config["generate_ssl"] and not config["domain"]:
            tips.append("SSL is enabled, but the domain is missing.")
        if not config["setup_production"]:
            tips.append("Production setup is turned off. Enable it if this is a live deployment.")
        if "System update" in self.current_step_title:
            tips.append("The system update step can take a long time, especially on the first run.")
            tips.append("Manual checks: `ps aux | grep apt`, `sudo lsof /var/lib/dpkg/lock-frontend`, or `sudo tail -n 120 /var/log/apt/history.log`.")
        if self.failed_steps:
            tips.append("A recent job failed. Compare the failed step with the terminal output and run the matching recovery command.")
        elif self.completed_steps and self.total_steps and self.completed_steps == self.total_steps:
            tips.append("Deployment looks complete. Verify nginx, supervisor, and site login next.")
        elif self.total_steps:
            tips.append(f"Deployment in progress: {self.completed_steps}/{self.total_steps} steps complete.")
        else:
            tips.append("No deployment has started yet. Save server details and start the flow from the bench page.")

        latest_logs = self._latest_console_lines()
        if latest_logs:
            tips.append("Latest logs:")
            tips.extend(f"- {line}" for line in latest_logs[-4:])

        if self.last_recovery_commands:
            commands = deserialize_recovery_commands(self.last_recovery_commands)
            first_recovery = commands[0].strip() if commands else ""
            if first_recovery:
                tips.append(f"Recovery shortcut available: {first_recovery}")

        return "\n".join(tips)

    def generate_codex_suggestion(self):
        suggestion = self._build_codex_suggestion(self.ai_prompt_input.text().strip())
        self.ai_suggestion_box.setPlainText(suggestion)

    def _manual_command_config(self):
        config = self.collect_config()
        if not config["remote"]:
            config["ssh_user"] = config["ssh_user"] or ""
        return config

    def save_server_profile(self):
        data = {
            "remote": self.remote_radio.isChecked(),
            "access_mode": "root" if self.root_radio.isChecked() else "user",
            "server_ip": self.server_ip.text().strip(),
            "ssh_port": self.ssh_port.value(),
            "ssh_user": self.ssh_user.text().strip(),
            "ssh_password": self.ssh_password.text(),
            "use_ssh_key": self.use_ssh_key_checkbox.isChecked(),
            "ssh_private_key": self.ssh_private_key.toPlainText().strip(),
            "ssh_key_passphrase": self.ssh_key_passphrase.text(),
            "ssh_public_key": self.ssh_public_key.toPlainText().strip(),
            "sudo_password": self.sudo_password.text(),
            "create_user": self.create_user_checkbox.isChecked(),
            "create_swap": self.create_swap_checkbox.isChecked(),
            "swap_size_gb": self.swap_size_gb.value(),
            "new_username": self.username.text().strip(),
            "new_password": self.password.text(),
            "bench_name": self.bench_name_input.text().strip(),
            "bench_version": self.bench_branch.currentText(),
            "create_site": self.create_site_checkbox.isChecked(),
            "site_name": self.site_name.text().strip(),
            "site_admin_password": self.site_admin_password.text().strip(),
            "domain": self.domain_name.text().strip(),
            "setup_production": self.setup_production_checkbox.isChecked(),
            "generate_ssl": self.generate_ssl_checkbox.isChecked(),
        }
        try:
            with open(self.settings_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            self._append_console_line(f"[INFO] Server info saved to {self.settings_path}")
            QtWidgets.QMessageBox.information(self, "Saved", f"Server info saved to:\n{self.settings_path}")
        except Exception as exc:
            self._append_console_line(f"[ERROR] Could not save server info: {exc}")
            QtWidgets.QMessageBox.warning(self, "Save Failed", str(exc))

    def load_server_profile(self):
        if not os.path.exists(self.settings_path):
            QtWidgets.QMessageBox.information(self, "No Saved Info", f"Saved profile not found:\n{self.settings_path}")
            return
        try:
            with open(self.settings_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            self._append_console_line(f"[ERROR] Could not load saved server info: {exc}")
            QtWidgets.QMessageBox.warning(self, "Load Failed", str(exc))
            return

        self.remote_radio.setChecked(bool(data.get("remote", False)))
        self.same_radio.setChecked(not bool(data.get("remote", False)))
        if data.get("access_mode") == "user":
            self.user_radio.setChecked(True)
        else:
            self.root_radio.setChecked(True)
        self.server_ip.setText(data.get("server_ip", ""))
        self.ssh_port.setValue(int(data.get("ssh_port", 22)))
        self.ssh_user.setText(data.get("ssh_user", ""))
        self.ssh_password.setText(data.get("ssh_password", ""))
        self.use_ssh_key_checkbox.setChecked(bool(data.get("use_ssh_key", False)))
        self.ssh_private_key.setPlainText(data.get("ssh_private_key", ""))
        self.ssh_key_passphrase.setText(data.get("ssh_key_passphrase", ""))
        self.ssh_public_key.setPlainText(data.get("ssh_public_key", ""))
        self.sudo_password.setText(data.get("sudo_password", ""))
        self.create_user_checkbox.setChecked(bool(data.get("create_user", False)))
        self.create_swap_checkbox.setChecked(bool(data.get("create_swap", False)))
        self.swap_size_gb.setValue(int(data.get("swap_size_gb", 4)))
        self.username.setText(data.get("new_username", ""))
        self.password.setText(data.get("new_password", ""))
        self.bench_name_input.setText(data.get("bench_name", ""))
        self.bench_branch.setCurrentText(data.get("bench_version", "version-15"))
        self.create_site_checkbox.setChecked(bool(data.get("create_site", False)))
        self.site_name.setText(data.get("site_name", ""))
        self.site_admin_password.setText(data.get("site_admin_password", ""))
        self.domain_name.setText(data.get("domain", ""))
        self.setup_production_checkbox.setChecked(bool(data.get("setup_production", False)))
        self.generate_ssl_checkbox.setChecked(bool(data.get("generate_ssl", False)))
        self.update_remote_fields()
        self.update_access_mode()
        self.update_user_fields()
        self._append_console_line(f"[INFO] Server info loaded from {self.settings_path}")
        QtWidgets.QMessageBox.information(self, "Loaded", f"Server info loaded from:\n{self.settings_path}")

    def append_log(self, message):
        try:
            if isinstance(message, dict):
                message_type = message.get("type")
                if message_type == "plan":
                    self.total_steps = message.get("total", 0)
                    self.completed_steps = 0
                    self.failed_steps = 0
                    self.process_list.clear()
                    self.step_items = {}
                    for index, title in enumerate(message.get("steps", []), start=1):
                        self._set_process_item(index, title, "pending", "Waiting to start")
                    self.process_overview_label.setText(
                        f"Process overview: 0/{self.total_steps} completed, 0 failed"
                    )
                    self._update_summary()
                    return
                if message_type == "progress":
                    self.progress_bar.setValue(message.get("value", 0))
                    return
                if message_type == "current_step":
                    title = message.get("title", "running")
                    command = message.get("command", "")
                    index = message.get("index", 0)
                    total = message.get("total", self.total_steps)
                    self.current_step_title = title
                    self.active_step_label.setText(f"Current step: {title}")
                    if index and total:
                        self.process_overview_label.setText(
                            f"Process overview: running step {index}/{total}"
                        )
                    if command:
                        self._append_console_line(f"[CMD] {command}")
                    return
                if message_type == "step_started":
                    self._set_process_item(
                        message.get("index", 0),
                        message.get("title", "Running"),
                        "running",
                        "Command started",
                    )
                    return
                if message_type == "step_output":
                    content = self._format_terminal_output(message.get("content", ""))
                    if content:
                        self._append_console_line(content)
                    return
                if message_type == "step_finished":
                    self.completed_steps = max(self.completed_steps, message.get("index", 0))
                    self._set_process_item(
                        message.get("index", 0),
                        message.get("title", "Completed"),
                        "done",
                        "Completed successfully",
                    )
                    self.process_overview_label.setText(
                        f"Process overview: {self.completed_steps}/{self.total_steps} completed, {self.failed_steps} failed"
                    )
                    self._update_summary()
                    return
                if message_type == "failed_step":
                    title = message.get("title", "Unknown step")
                    details = message.get("error", "")
                    self.failed_steps = 1
                    self._set_process_item(
                        message.get("index", 0),
                        title,
                        "failed",
                        details or f"Exit code {message.get('code', 'unknown')}",
                    )
                    self.process_overview_label.setText(
                        f"Process overview: failed on step {message.get('index', 0)}/{message.get('total', self.total_steps)}"
                    )
                    self.result_value.setText("Failed")
                    self._update_summary()
                    self._append_console_line(f"[FAILED STEP] {title}: {details}")
                    return

            self._append_console_line(str(message))
        except Exception as exc:
            self._append_console_line(f"[UI ERROR] {exc}")

    def show_validation_error(self, message):
        self.append_log(f"[VALIDATION] {message}")
        QtWidgets.QMessageBox.warning(self, "Validation Error", message)

    def update_access_mode(self):
        is_remote = self.remote_radio.isChecked()
        use_wsl = self.is_windows_host and not is_remote
        if use_wsl:
            self.root_radio.setChecked(True)
            self.root_radio.setEnabled(False)
            self.user_radio.setEnabled(False)
            self.ssh_user.setText("root")
            self.ssh_user.setEnabled(False)
            self.sudo_password.clear()
            self.sudo_password.setEnabled(False)
            self._refresh_terminal_prompt()
            return

        self.root_radio.setEnabled(True)
        self.user_radio.setEnabled(True)
        if self.root_radio.isChecked():
            self.ssh_user.setText("root" if is_remote else "")
            self.ssh_user.setEnabled(False)
        else:
            self.ssh_user.setEnabled(is_remote)
        self.sudo_password.setEnabled(is_remote)
        self._refresh_terminal_prompt()

    def update_remote_fields(self):
        is_remote = self.remote_radio.isChecked()
        is_user_mode = self.user_radio.isChecked()
        use_wsl = self.is_windows_host and not is_remote

        self.server_ip.setEnabled(is_remote)
        self.ssh_port.setEnabled(is_remote)
        use_ssh_key = self.use_ssh_key_checkbox.isChecked()
        self.ssh_password.setEnabled(is_remote and not use_ssh_key)
        self.use_ssh_key_checkbox.setEnabled(is_remote)
        self.ssh_private_key.setEnabled(is_remote and use_ssh_key)
        self.ssh_key_passphrase.setEnabled(is_remote and use_ssh_key)
        self.ssh_public_key.setEnabled(is_remote)
        self.sudo_password.setEnabled(is_remote)
        self.ssh_user.setEnabled(is_remote and is_user_mode)
        self.setup_production_checkbox.setEnabled(is_remote)
        self.generate_ssl_checkbox.setEnabled(is_remote)
        if not is_remote:
            self.setup_production_checkbox.setChecked(False)
            self.generate_ssl_checkbox.setChecked(False)

        if is_remote and self.root_radio.isChecked():
            self.ssh_user.setText("root")
        if not is_remote:
            self.server_ip.clear()
            self.ssh_password.clear()
            self.use_ssh_key_checkbox.setChecked(False)
            self.ssh_private_key.clear()
            self.ssh_key_passphrase.clear()
            self.ssh_public_key.clear()
            if use_wsl:
                self.ssh_user.setText("root")
            elif self.root_radio.isChecked():
                self.ssh_user.clear()

        if use_wsl:
            self.location_note.setText("Windows local deployment uses WSL. Switch to Remote Server to deploy on a VPS.")
        elif is_remote:
            self.location_note.setText("Remote mode connects over SSH and runs commands on the Ubuntu target.")
        elif self.is_linux_host:
            self.location_note.setText(f"Same System mode runs directly on this machine: {self.local_linux_name or 'Linux'}.")
        else:
            self.location_note.setText("Same System mode runs commands directly on this machine.")

        self._refresh_monitor_labels()
        self.update_access_mode()
        self._refresh_terminal_prompt()

    def update_user_fields(self):
        enabled = self.create_user_checkbox.isChecked()
        self.username.setEnabled(enabled)
        self.password.setEnabled(enabled)

    def update_ssl_state(self):
        if not self.remote_radio.isChecked():
            self.generate_ssl_checkbox.setChecked(False)
            self.setup_production_checkbox.setChecked(False)
            return
        if self.generate_ssl_checkbox.isChecked() and not self.setup_production_checkbox.isChecked():
            self.setup_production_checkbox.setChecked(True)

    def _current_bench_name(self):
        custom_name = self.bench_name_input.text().strip()
        if custom_name:
            return custom_name
        bench_version = self.bench_branch.currentText().strip() or "version-15"
        return f"bench-{bench_version.split('-')[-1]}"

    def _current_node_version(self):
        return normalize_node_version(BENCH_NODE_MAP.get(self.bench_branch.currentText().strip() or "version-15", "18"))

    def _with_current_nvm(self, command):
        if not self.use_nvm_checkbox.isChecked():
            return command
        node_version = self._current_node_version()
        return (
            'export NVM_DIR="$HOME/.nvm" && '
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && '
            f"nvm install {node_version} >/dev/null && "
            f"nvm use {node_version} >/dev/null && "
            f"{command}"
        )

    def _run_troubleshoot_command(self, command):
        self.process_terminal.set_current_command(command)
        self.switch_section(3)
        self.run_manual_command(command)

    def run_health_check(self):
        self._refresh_monitor_labels()
        self._run_troubleshoot_command(self._with_current_nvm(self._monitor_command()))

    def run_generate_ssl_now(self):
        config = self.collect_config()
        selected_sites = self._selected_site_items()
        if selected_sites and len(selected_sites) != 1:
            self._append_process_terminal_line("[VALIDATION] Select exactly one site for this shortcut, or use the Generate SSL button in the site list.")
            return
        selected_site = selected_sites[0] if selected_sites else None
        domain = config["domain"] or (selected_site or {}).get("site", "")
        if not domain:
            self._append_process_terminal_line("[VALIDATION] Domain is required before generating SSL.")
            return
        bench_path = (selected_site or {}).get("bench_path") or self._current_bench_name()
        site_name = (selected_site or {}).get("site") or config["site_name"]
        if not site_name:
            self._append_process_terminal_line("[VALIDATION] Select a site or fill Site Name before generating SSL.")
            return
        cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"
        command = (
            "sudo apt-get install -y certbot python3-certbot-nginx && "
            f"if [ -f {shell_quote(cert_path)} ] && [ -f {shell_quote(key_path)} ]; then "
            f"echo 'SSL certificate already exists for {domain}'; "
            f"else sudo certbot --nginx -d {shell_quote(domain)} --non-interactive --agree-tos -m {shell_quote(f'admin@{domain}')}; fi "
            f"&& cd {shell_quote(bench_path)} && bench --site {shell_quote(site_name)} set-config ssl_certificate {shell_quote(cert_path)} "
            f"&& bench --site {shell_quote(site_name)} set-config ssl_certificate_key {shell_quote(key_path)}"
        )
        self._run_troubleshoot_command(command)

    def run_sync_ssl_paths(self):
        config = self.collect_config()
        selected_sites = self._selected_site_items()
        if selected_sites and len(selected_sites) != 1:
            self._append_process_terminal_line("[VALIDATION] Select exactly one site before syncing SSL paths.")
            return
        selected_site = selected_sites[0] if selected_sites else None
        site_name = (selected_site or {}).get("site") or config["site_name"]
        bench_path = (selected_site or {}).get("bench_path") or self._current_bench_name()
        domain = config["domain"] or site_name
        if not site_name or not domain:
            self._append_process_terminal_line("[VALIDATION] Site Name and Domain are required for SSL path sync.")
            return
        cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"
        command = (
            f"if [ -f {shell_quote(cert_path)} ] && [ -f {shell_quote(key_path)} ]; then "
            f"cd {shell_quote(bench_path)} && bench --site {shell_quote(site_name)} set-config ssl_certificate {shell_quote(cert_path)} "
            f"&& bench --site {shell_quote(site_name)} set-config ssl_certificate_key {shell_quote(key_path)}; "
            "else echo 'Certificate files are missing for this domain.'; fi"
        )
        self._run_troubleshoot_command(self._with_current_nvm(command))

    def run_bench_migrate(self):
        config = self.collect_config()
        selected_sites = self._selected_site_items()
        if selected_sites and len(selected_sites) != 1:
            self._append_process_terminal_line("[VALIDATION] Select exactly one site for this shortcut, or use the Migrate button in the site list.")
            return
        selected_site = selected_sites[0] if selected_sites else None
        site_name = (selected_site or {}).get("site") or config["site_name"]
        bench_path = (selected_site or {}).get("bench_path") or self._current_bench_name()
        if not site_name:
            self._append_process_terminal_line("[VALIDATION] Site Name is required for bench migrate.")
            return
        command = f"cd {shell_quote(bench_path)} && bench --site {shell_quote(site_name)} migrate"
        self._run_troubleshoot_command(self._with_current_nvm(command))

    def run_bench_restart(self):
        bench_name = self._current_bench_name()
        command = (
            "sudo supervisorctl reread && sudo supervisorctl update && "
            f"(sudo supervisorctl restart {bench_name}-redis:* {bench_name}-workers:* {bench_name}-web:{bench_name}-frappe-web || true) && "
            f"(sudo supervisorctl restart {bench_name}-web:{bench_name}-node-socketio || true) && "
            f"echo 'Supervisor status for {bench_name}:' && sudo supervisorctl status | grep {shell_quote(bench_name)} && "
            "sudo systemctl reload nginx"
        )
        self._run_troubleshoot_command(command)

    def run_production_rerun(self):
        config = self.collect_config()
        bench_name = self._current_bench_name()
        production_user = config.get("frappe_user") or config["ssh_user"] or "root"
        command = (
            'BENCH_BIN="$(command -v bench 2>/dev/null || true)" '
            '&& if [ -z "$BENCH_BIN" ] && [ -x "$HOME/.local/bin/bench" ]; then BENCH_BIN="$HOME/.local/bin/bench"; fi '
            '&& if [ -z "$BENCH_BIN" ]; then echo "bench binary not found"; exit 1; fi '
            f'&& cd {bench_name} && yes y | sudo "$BENCH_BIN" setup production {production_user}'
        )
        self._run_troubleshoot_command(self._with_current_nvm(command))

    def run_server_restart(self):
        self._run_troubleshoot_command("sudo reboot")

    def collect_config(self):
        use_wsl = self.is_windows_host and not self.remote_radio.isChecked()
        is_remote = self.remote_radio.isChecked()
        return {
            "remote": is_remote,
            "use_wsl": use_wsl,
            "access_mode": "root" if self.root_radio.isChecked() else "user",
            "server_ip": self.server_ip.text().strip(),
            "ssh_port": self.ssh_port.value(),
            "ssh_user": "root" if use_wsl else self.ssh_user.text().strip(),
            "ssh_password": self.ssh_password.text(),
            "use_ssh_key": False if use_wsl else self.use_ssh_key_checkbox.isChecked(),
            "ssh_private_key": "" if use_wsl else self.ssh_private_key.toPlainText().strip(),
            "ssh_key_passphrase": "" if use_wsl else self.ssh_key_passphrase.text(),
            "ssh_public_key": "" if use_wsl else self.ssh_public_key.toPlainText().strip(),
            "create_user": self.create_user_checkbox.isChecked(),
            "create_swap": self.create_swap_checkbox.isChecked(),
            "swap_size_gb": self.swap_size_gb.value(),
            "new_username": self.username.text().strip(),
            "new_password": self.password.text(),
            "frappe_user": self.username.text().strip() if self.create_user_checkbox.isChecked() else self.ssh_user.text().strip(),
            "bench_name": self.bench_name_input.text().strip(),
            "create_site": self.create_site_checkbox.isChecked(),
            "site_name": self.site_name.text().strip(),
            "site_admin_password": self.site_admin_password.text().strip() or "admin",
            "mariadb_root_password": self.mariadb_root_password.text().strip() or "root",
            "current_mariadb_root_password": self.current_mariadb_root_password.text().strip(),
            "mysql_mode": (
                "reinstall" if self.mysql_reinstall_radio.isChecked()
                else "keep" if self.mysql_keep_radio.isChecked()
                else "reset"
            ),
            "domain": self.domain_name.text().strip(),
            "bench_version": self.bench_branch.currentText(),
            "use_nvm": self.use_nvm_checkbox.isChecked(),
            "sudo_password": self.sudo_password.text(),
            "setup_production": is_remote and self.setup_production_checkbox.isChecked(),
            "generate_ssl": is_remote and self.generate_ssl_checkbox.isChecked(),
        }

    def validate_config(self, config):
        if config.get("create_site") and not config["site_name"]:
            return "Site Name is required when Create site is enabled."
        if config["generate_ssl"] and not config["domain"]:
            return "Domain is required when SSL is enabled."
        if config["remote"]:
            if not config["server_ip"]:
                return "Server IP is required for remote deployment."
            if int(config.get("ssh_port", 22)) == 1:
                return "SSH port `1` looks incorrect. Use `22` unless your server uses a custom SSH port."
            if config.get("use_ssh_key") and not config.get("ssh_private_key"):
                return "SSH private key is required when SSH key login is enabled."
            if config.get("use_ssh_key") and config.get("ssh_private_key", "").startswith("ssh-"):
                return "That looks like a public key. SSH key login needs the private key that starts with `-----BEGIN`."
            if not config.get("use_ssh_key") and not config["ssh_password"]:
                return "SSH password is required for remote deployment."
            if config["access_mode"] == "user" and not config["ssh_user"]:
                return "SSH username is required for user access mode."
            if config["access_mode"] == "user" and not config.get("sudo_password"):
                return "Sudo Password is required for user access mode."
        if config["create_user"] and (not config["new_username"] or not config["new_password"]):
            return "Username and password are required when Create New User is enabled."
        return None

    def validate_connection_config(self, config):
        if config["remote"]:
            if not config["server_ip"]:
                return "Server IP is required for manual commands."
            if int(config.get("ssh_port", 22)) == 1:
                return "SSH port `1` looks incorrect for manual commands. Use `22` unless your server uses a custom SSH port."
            if config.get("use_ssh_key") and not config.get("ssh_private_key"):
                return "SSH private key is required for manual commands when SSH key login is enabled."
            if config.get("use_ssh_key") and config.get("ssh_private_key", "").startswith("ssh-"):
                return "That looks like a public key. SSH key login needs the private key that starts with `-----BEGIN`."
            if not config.get("use_ssh_key") and not config["ssh_password"]:
                return "SSH password is required for manual commands."
            if config["access_mode"] == "user" and not config["ssh_user"]:
                return "SSH username is required for manual commands."
            if config["access_mode"] == "user" and not config.get("sudo_password"):
                return "Sudo Password is required for manual commands."
        return None

    def copy_recovery(self):
        if not self.last_recovery_commands:
            return
        QtWidgets.QApplication.clipboard().setText(self.last_recovery_commands)
        self.append_log("[INFO] Recovery commands copied to clipboard.")

    def update_backup_button_state(self):
        has_checked = any(
            self.site_list.item(index).checkState() == QtCore.Qt.Checked
            for index in range(self.site_list.count())
        )
        self.take_backup_button.setEnabled(has_checked)
        self.migrate_site_button.setEnabled(has_checked)
        self.generate_site_ssl_button.setEnabled(has_checked)

    def load_server_sites(self):
        if self.site_thread is not None:
            self._append_process_terminal_line("[INFO] Site loading is already in progress.")
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            return

        self.load_sites_button.setEnabled(False)
        self.migrate_site_button.setEnabled(False)
        self.generate_site_ssl_button.setEnabled(False)
        self.take_backup_button.setEnabled(False)
        self.download_backup_button.setEnabled(False)
        self.upload_backup_button.setEnabled(False)
        self.site_list.clear()
        self.available_sites = []
        self.switch_section(3)

        self.site_thread = QtCore.QThread(self)
        self.site_worker = SiteInventoryWorker(config)
        self.site_worker.moveToThread(self.site_thread)
        self.site_worker.log_message.connect(self._append_process_terminal_line)
        self.site_worker.finished.connect(self.on_sites_loaded)
        self.site_worker.finished.connect(self.site_thread.quit)
        self.site_thread.finished.connect(self.site_worker.deleteLater)
        self.site_thread.finished.connect(self.site_thread.deleteLater)
        self.site_thread.started.connect(self.site_worker.run)
        self.site_thread.start()

    def on_sites_loaded(self, sites, error):
        try:
            self.available_sites = sites or []
            self.site_list.clear()
            if error:
                self._append_process_terminal_line(f"[FAILED] {error}")
                return
            for item_data in self.available_sites:
                label = f"{item_data['site']} ({item_data['bench']})"
                item = QtWidgets.QListWidgetItem(label)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Unchecked)
                item.setData(QtCore.Qt.UserRole, item_data)
                self.site_list.addItem(item)
            if self.available_sites:
                self._append_process_terminal_line(f"[DONE] Loaded {len(self.available_sites)} site(s) from the server.")
            else:
                self._append_process_terminal_line("[INFO] No sites were found on the server.")
        finally:
            self.load_sites_button.setEnabled(True)
            self.update_backup_button_state()
            self.download_backup_button.setEnabled(bool(self.latest_backup_manifest))
            self.upload_backup_button.setEnabled(True)
            self.site_worker = None
            self.site_thread = None

    def _selected_site_items(self):
        selected = []
        for index in range(self.site_list.count()):
            item = self.site_list.item(index)
            if item.checkState() != QtCore.Qt.Checked:
                continue
            payload = item.data(QtCore.Qt.UserRole)
            if payload:
                selected.append(payload)
        return selected

    def _run_selected_site_action(self, action, label):
        selected_sites = self._selected_site_items()
        if not selected_sites:
            self._append_process_terminal_line("[INFO] Select at least one site from the Server Sites list.")
            return
        if self.site_action_thread is not None:
            self._append_process_terminal_line("[INFO] A site action is already running.")
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            return

        if action == "ssl" and config.get("domain") and len(selected_sites) == 1:
            selected_sites = [dict(selected_sites[0], domain=config["domain"])]

        self.migrate_site_button.setEnabled(False)
        self.generate_site_ssl_button.setEnabled(False)
        self.take_backup_button.setEnabled(False)
        self.switch_section(3)
        self._append_process_terminal_line(f"$ Starting {label.lower()} for {len(selected_sites)} selected site(s)...")

        self.site_action_thread = QtCore.QThread(self)
        self.site_action_worker = SiteActionWorker(config, selected_sites, action)
        self.site_action_worker.moveToThread(self.site_action_thread)
        self.site_action_worker.log_message.connect(self._append_process_terminal_line)
        self.site_action_worker.finished.connect(self.on_selected_site_action_finished)
        self.site_action_worker.finished.connect(self.site_action_thread.quit)
        self.site_action_thread.finished.connect(self.site_action_worker.deleteLater)
        self.site_action_thread.finished.connect(self.site_action_thread.deleteLater)
        self.site_action_thread.started.connect(self.site_action_worker.run)
        self.site_action_thread.start()

    def migrate_selected_sites(self):
        self._run_selected_site_action("migrate", "Migrate")

    def generate_ssl_for_selected_sites(self):
        self._run_selected_site_action("ssl", "Generate SSL")

    def on_selected_site_action_finished(self, error):
        try:
            if error:
                self._append_process_terminal_line(f"[FAILED] {error}")
            else:
                self._append_process_terminal_line("[DONE] Selected site action completed.")
        finally:
            self.update_backup_button_state()
            self.site_action_worker = None
            self.site_action_thread = None

    def take_selected_site_backups(self):
        selected_sites = self._selected_site_items()
        if not selected_sites:
            self._append_process_terminal_line("[INFO] Select at least one site from the Server Sites list.")
            return
        if self.backup_thread is not None:
            self._append_process_terminal_line("[INFO] A backup task is already running.")
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            return

        self.take_backup_button.setEnabled(False)
        self.download_backup_button.setEnabled(False)
        self.switch_section(3)
        self._append_process_terminal_line(f"$ Starting backups for {len(selected_sites)} selected site(s)...")

        self.backup_thread = QtCore.QThread(self)
        self.backup_worker = SiteBackupWorker(config, selected_sites)
        self.backup_worker.moveToThread(self.backup_thread)
        self.backup_worker.log_message.connect(self._append_process_terminal_line)
        self.backup_worker.finished.connect(self.on_site_backups_finished)
        self.backup_worker.finished.connect(self.backup_thread.quit)
        self.backup_thread.finished.connect(self.backup_worker.deleteLater)
        self.backup_thread.finished.connect(self.backup_thread.deleteLater)
        self.backup_thread.started.connect(self.backup_worker.run)
        self.backup_thread.start()

    def on_site_backups_finished(self, manifest, error):
        try:
            self.latest_backup_manifest = manifest or []
            if error:
                self._append_process_terminal_line(f"[FAILED] {error}")
                return
            total_files = sum(len(entry.get("files", [])) for entry in self.latest_backup_manifest)
            self._append_process_terminal_line(
                f"[DONE] Backup files prepared for {len(self.latest_backup_manifest)} site(s), {total_files} file(s) ready."
            )
        finally:
            self.update_backup_button_state()
            self.download_backup_button.setEnabled(bool(self.latest_backup_manifest))
            self.upload_backup_button.setEnabled(True)
            self.backup_worker = None
            self.backup_thread = None

    def download_site_backups(self):
        if not self.latest_backup_manifest:
            self._append_process_terminal_line("[INFO] No backup files are ready to download yet.")
            return
        if self.download_thread is not None:
            self._append_process_terminal_line("[INFO] A backup download is already running.")
            return

        destination_root = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose Backup Download Folder",
            os.path.join(self.project_root, "downloaded_backups"),
        )
        if not destination_root:
            self._append_process_terminal_line("[INFO] Backup download cancelled.")
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            return

        self.download_backup_button.setEnabled(False)
        self.switch_section(3)
        self._append_process_terminal_line(f"$ Downloading backups to {destination_root}")

        self.download_thread = QtCore.QThread(self)
        self.download_worker = SiteDownloadWorker(config, self.latest_backup_manifest, destination_root)
        self.download_worker.moveToThread(self.download_thread)
        self.download_worker.log_message.connect(self._append_process_terminal_line)
        self.download_worker.finished.connect(self.on_backup_download_finished)
        self.download_worker.finished.connect(self.download_thread.quit)
        self.download_thread.finished.connect(self.download_worker.deleteLater)
        self.download_thread.finished.connect(self.download_thread.deleteLater)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_thread.start()

    def on_backup_download_finished(self, error):
        try:
            if error:
                self._append_process_terminal_line(f"[FAILED] {error}")
            else:
                self._append_process_terminal_line("[DONE] Backup download completed.")
        finally:
            self.download_backup_button.setEnabled(bool(self.latest_backup_manifest))
            self.download_worker = None
            self.download_thread = None

    def upload_backup_folder(self):
        if self.upload_thread is not None:
            self._append_process_terminal_line("[INFO] A backup upload is already running.")
            return

        source_root = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose Backup Folder To Upload",
            os.path.join(self.project_root, "downloaded_backups"),
        )
        if not source_root:
            self._append_process_terminal_line("[INFO] Backup upload cancelled.")
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            return

        self.upload_backup_button.setEnabled(False)
        self.switch_section(3)
        self._append_process_terminal_line(f"$ Uploading backup folder {source_root}")

        self.upload_thread = QtCore.QThread(self)
        self.upload_worker = SiteUploadWorker(config, source_root)
        self.upload_worker.moveToThread(self.upload_thread)
        self.upload_worker.log_message.connect(self._append_process_terminal_line)
        self.upload_worker.finished.connect(self.on_backup_upload_finished)
        self.upload_worker.finished.connect(self.upload_thread.quit)
        self.upload_thread.finished.connect(self.upload_worker.deleteLater)
        self.upload_thread.finished.connect(self.upload_thread.deleteLater)
        self.upload_thread.started.connect(self.upload_worker.run)
        self.upload_thread.start()

    def on_backup_upload_finished(self, error):
        try:
            if error:
                self._append_process_terminal_line(f"[FAILED] {error}")
            else:
                self._append_process_terminal_line("[DONE] Backup upload completed.")
        finally:
            self.upload_backup_button.setEnabled(True)
            self.upload_worker = None
            self.upload_thread = None

    def use_last_recovery_command(self):
        if not self.last_recovery_commands:
            self._append_process_terminal_line("[INFO] No recovery command available yet.")
            return
        commands = deserialize_recovery_commands(self.last_recovery_commands)
        first_command = commands[0].strip() if commands else ""
        if not first_command:
            self._append_process_terminal_line("[INFO] No recovery command available yet.")
            return
        self.process_terminal.set_current_command(first_command)
        self._append_process_terminal_line(f"$ Loaded recovery command: {first_command}")

    def on_resume_deploy(self):
        if not self.last_recovery_commands:
            self._append_process_terminal_line("[INFO] No resume plan available.")
            return
        if self.worker_thread is not None:
            self._append_process_terminal_line("[INFO] A deployment task is already running.")
            return

        config = self.collect_config()
        validation_error = self.validate_config(config)
        if validation_error:
            self.show_validation_error(validation_error)
            return

        self.switch_section(3)
        self.run_button.setEnabled(False)
        self.recovery_button.setEnabled(False)
        self.deployment_pause_pending = False
        self.progress_bar.setValue(0)
        self.active_step_label.setText("Current step: resuming deployment")
        self._set_status_chip("Resuming", "#e8f4f0", "#176653", "#cbe7de")
        self.result_value.setText("Resuming")
        self.process_list.clear()
        self.step_items = {}
        self._append_process_terminal_line("$ Resuming deployment from saved recovery plan...")

        self.worker_thread = QtCore.QThread(self)
        self.worker = DeploymentWorker(config, recovery_commands=self.last_recovery_commands)
        self.worker.moveToThread(self.worker_thread)
        self.worker.log_message.connect(self.append_log)
        self.worker.finished.connect(self.on_deployment_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()

    def pause_deployment(self):
        if self.worker is None:
            self._append_process_terminal_line("[INFO] No deployment is running.")
            return
        self.deployment_pause_pending = True
        self.worker.request_pause()

    def run_manual_command(self, command=None):
        command = (command or self.process_terminal.current_command()).strip()
        if not command:
            self._append_process_terminal_line("[INFO] Enter a command first.")
            self.process_terminal.enable_prompt("")
            return
        terminal_shortcut = command.lower()
        if terminal_shortcut in {"/resume", "resume"}:
            had_worker = self.worker_thread is not None
            self.on_resume_deploy()
            if not had_worker and self.worker_thread is None:
                self.process_terminal.enable_prompt("")
            return
        if terminal_shortcut in {"/pause", "pause"}:
            self.pause_deployment()
            self.process_terminal.enable_prompt("")
            return
        if terminal_shortcut in {"/recovery", "recovery"}:
            self.use_last_recovery_command()
            return
        if "tail -f" in command:
            command = command.replace("tail -f", "tail -n 120")
            self.process_terminal.set_current_command(command)
            self._append_process_terminal_line("[INFO] Replaced `tail -f` with `tail -n 120` so the manual command can finish.")
        if self.command_thread is not None:
            self._append_process_terminal_line("[INFO] A manual command is already running.")
            self.process_terminal.enable_prompt(command)
            return

        config = self._manual_command_config()
        validation_error = self.validate_connection_config(config)
        if validation_error:
            self._append_process_terminal_line(f"[VALIDATION] {validation_error}")
            self.process_terminal.enable_prompt(command)
            return

        self.process_terminal.disable_prompt()
        self.switch_section(3)
        wrapped_command = self._wrap_manual_command(command, config)

        self.command_thread = QtCore.QThread(self)
        self.command_worker = ManualCommandWorker(config, wrapped_command)
        self.command_worker.moveToThread(self.command_thread)
        self.command_worker.finished.connect(self.on_manual_command_finished)
        self.command_worker.finished.connect(self.command_thread.quit)
        self.command_thread.finished.connect(self.command_worker.deleteLater)
        self.command_thread.finished.connect(self.command_thread.deleteLater)
        self.command_thread.started.connect(self.command_worker.run)
        self.command_thread.start()

    def on_manual_command_finished(self, code, out, err):
        try:
            detected_cwd = self._extract_manual_cwd(out, err)
            if detected_cwd:
                self.manual_terminal_cwd = detected_cwd

            stdout_text = self._format_terminal_output(self._remove_manual_cwd_marker(out))
            stderr_text = self._format_terminal_output(self._remove_manual_cwd_marker(err))
            if stdout_text:
                self._append_process_terminal_line(stdout_text)
            if stderr_text:
                self._append_process_terminal_line(stderr_text)
            if code != 0:
                self._append_process_terminal_line(f"[FAILED] Exit code {code}")
        finally:
            self.process_terminal.enable_prompt("")
            self.command_worker = None
            self.command_thread = None

    def on_deploy(self):
        config = self.collect_config()
        validation_error = self.validate_config(config)
        if validation_error:
            self.show_validation_error(validation_error)
            return

        self.switch_section(3)
        self.run_button.setEnabled(False)
        self.recovery_button.setEnabled(False)
        self.last_recovery_commands = ""
        self.deployment_pause_pending = False
        self._reset_runtime_state()
        self.manual_terminal_cwd = self._default_terminal_cwd(config)
        self.progress_bar.setValue(0)
        self.active_step_label.setText("Current step: preparing deployment")
        self._set_status_chip("Running", "#e8f4f0", "#176653", "#cbe7de")
        self.result_value.setText("Running")
        self.process_terminal.set_prompt(self._terminal_prompt_text(config))
        self.log_console.reset_terminal()
        self.append_log("$ Starting deployment...")

        self.worker_thread = QtCore.QThread(self)
        self.worker = DeploymentWorker(config)
        self.worker.moveToThread(self.worker_thread)
        self.worker.log_message.connect(self.append_log)
        self.worker.finished.connect(self.on_deployment_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()

    def on_deployment_finished(self, success, recovery):
        try:
            self.run_button.setEnabled(True)
            was_paused = (not success) and bool(recovery) and self.deployment_pause_pending
            if success:
                self._set_status_chip("Completed", "#edf7f4", "#176653", "#cbe7de")
                self.result_value.setText("Success")
            elif was_paused:
                self._set_status_chip("Paused", "#fff8e6", "#8a5a00", "#f3d58b")
                self.result_value.setText("Paused")
            else:
                self._set_status_chip("Needs Attention", "#fff1f0", "#a63f3f", "#f1c7c3")
                self.result_value.setText("Failed")
            if success:
                self.progress_bar.setValue(100)
                self.active_step_label.setText("Current step: done")
                self.process_overview_label.setText(
                    f"Process overview: {self.completed_steps}/{self.total_steps} completed successfully"
                )
                self.append_log("$ Deployment finished successfully.")
            elif was_paused:
                self.active_step_label.setText("Current step: paused")
                self.append_log("$ Deployment paused. Use Resume Deployment to continue.")
            else:
                self.active_step_label.setText("Current step: failed")
                self.append_log("$ Deployment stopped before completion.")
            self._update_summary()
            self.last_recovery_commands = recovery
            self.recovery_button.setEnabled(bool(recovery))
            self.deployment_pause_pending = False
        finally:
            self.process_terminal.enable_prompt("")
            self.worker = None
            self.worker_thread = None


def run_app():
    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setFont(QtGui.QFont("Segoe UI", 10))
        palette = app.palette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#eef3f6"))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#ffffff"))
        palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#f8fbfb"))
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#17313b"))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#17313b"))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#1d7a64"))
        palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#ffffff"))
        app.setPalette(palette)
        window = MainWindow()
        window.show()
        return app.exec()
    except Exception as exc:
        traceback.print_exc()
        QtWidgets.QMessageBox.critical(None, "Application Error", f"Unable to start application:\n{exc}")
        return 1
