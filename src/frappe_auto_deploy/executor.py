import getpass
import io
import os
import select
import shlex
import shutil
import subprocess

import paramiko


class Executor:
    WSL_DISTRO = "Ubuntu-22.04"

    def __init__(
        self,
        remote=False,
        host=None,
        port=22,
        username=None,
        password=None,
        use_wsl=False,
        use_ssh_key=False,
        ssh_private_key="",
        ssh_key_path="",
        ssh_key_passphrase="",
    ):
        self.remote = remote
        self.use_wsl = use_wsl
        if remote:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs = {
                "hostname": host,
                "port": port,
                "username": username,
                "timeout": 30,
                "look_for_keys": False,
                "allow_agent": False,
            }
            if use_ssh_key:
                connect_kwargs.update(self._ssh_key_connect_kwargs(ssh_private_key, ssh_key_path, ssh_key_passphrase))
            else:
                connect_kwargs["password"] = password
            self.client.connect(**connect_kwargs)
        else:
            self.client = None

    @staticmethod
    def _load_private_key_from_text(key_text, passphrase=None):
        key_error = None
        key_classes = [
            getattr(paramiko, "Ed25519Key", None),
            getattr(paramiko, "RSAKey", None),
            getattr(paramiko, "ECDSAKey", None),
            getattr(paramiko, "DSSKey", None),
        ]
        for key_class in [candidate for candidate in key_classes if candidate is not None]:
            try:
                return key_class.from_private_key(io.StringIO(key_text), password=passphrase or None)
            except paramiko.PasswordRequiredException:
                raise
            except paramiko.SSHException as exc:
                key_error = exc
        raise paramiko.SSHException(f"Unable to read SSH private key: {key_error}")

    def _ssh_key_connect_kwargs(self, ssh_private_key="", ssh_key_path="", ssh_key_passphrase=""):
        passphrase = ssh_key_passphrase or None
        key_text = str(ssh_private_key or "").strip()
        key_path = str(ssh_key_path or "").strip()
        if key_text:
            return {"pkey": self._load_private_key_from_text(key_text, passphrase)}
        if key_path:
            return {"key_filename": key_path, "passphrase": passphrase}
        raise ValueError("SSH private key or SSH key path is required when SSH key login is enabled.")

    @staticmethod
    def local_user_fallback():
        return getpass.getuser() or "root"

    def has_wsl_distro(self):
        completed = subprocess.run(
            ["wsl.exe", "--list", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            return False, completed.stderr.strip() or completed.stdout.strip()

        stdout = completed.stdout.replace("\x00", "")
        distros = [line.strip().replace("(Default)", "").strip() for line in stdout.splitlines() if line.strip()]
        return any(distro == self.WSL_DISTRO or distro.startswith(f"{self.WSL_DISTRO} ") for distro in distros), ""

    def install_wsl_distro(self):
        return subprocess.run(
            ["wsl.exe", "--install", "-d", self.WSL_DISTRO],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def detect_wsl_default_user(self):
        completed = subprocess.run(
            ["wsl.exe", "-d", self.WSL_DISTRO, "--", "bash", "-lc", "id -un"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()

    def run(self, cmd, check=True, stream_callback=None):
        if self.remote:
            stdin, stdout, stderr = self.client.exec_command(cmd, get_pty=True)
            channel = stdout.channel
            out_chunks = []
            err_chunks = []

            while True:
                read_ready, _, _ = select.select([channel], [], [], 0.2)
                if channel in read_ready:
                    if channel.recv_ready():
                        data = channel.recv(4096).decode("utf-8", errors="ignore")
                        if data:
                            out_chunks.append(data)
                            if stream_callback:
                                stream_callback("stdout", data)
                    if channel.recv_stderr_ready():
                        data = channel.recv_stderr(4096).decode("utf-8", errors="ignore")
                        if data:
                            err_chunks.append(data)
                            if stream_callback:
                                stream_callback("stderr", data)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

            code = channel.recv_exit_status()
            return code, "".join(out_chunks), "".join(err_chunks)
        if self.use_wsl:
            process = subprocess.Popen(
                ["wsl.exe", "-d", self.WSL_DISTRO, "-u", "root", "--", "bash", "-lc", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            out, err = self._stream_subprocess(process, stream_callback)
            return process.returncode, out, err
        else:
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            out, err = self._stream_subprocess(process, stream_callback)
            return process.returncode, out, err

    def _stream_subprocess(self, process, stream_callback=None):
        out_chunks = []
        err_chunks = []

        while True:
            stdout_line = process.stdout.readline() if process.stdout else ""
            stderr_line = process.stderr.readline() if process.stderr else ""

            if stdout_line:
                out_chunks.append(stdout_line)
                if stream_callback:
                    stream_callback("stdout", stdout_line)
            if stderr_line:
                err_chunks.append(stderr_line)
                if stream_callback:
                    stream_callback("stderr", stderr_line)

            if not stdout_line and not stderr_line and process.poll() is not None:
                break

        remaining_out = process.stdout.read() if process.stdout else ""
        remaining_err = process.stderr.read() if process.stderr else ""
        if remaining_out:
            out_chunks.append(remaining_out)
            if stream_callback:
                stream_callback("stdout", remaining_out)
        if remaining_err:
            err_chunks.append(remaining_err)
            if stream_callback:
                stream_callback("stderr", remaining_err)

        return "".join(out_chunks), "".join(err_chunks)

    def download_file(self, source_path, destination_path):
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        if self.remote:
            sftp = self.client.open_sftp()
            try:
                sftp.get(source_path, destination_path)
            finally:
                sftp.close()
            return
        if self.use_wsl:
            completed = subprocess.run(
                ["wsl.exe", "-d", self.WSL_DISTRO, "-u", "root", "--", "bash", "-lc", f"cat {source_path}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.decode("utf-8", errors="ignore") or "Unable to download file from WSL")
            with open(destination_path, "wb") as handle:
                handle.write(completed.stdout)
            return
        shutil.copy2(source_path, destination_path)

    def upload_file(self, source_path, destination_path):
        parent_dir = os.path.dirname(destination_path)
        if self.remote:
            sftp = self.client.open_sftp()
            try:
                self._sftp_mkdir_p(sftp, parent_dir)
                sftp.put(source_path, destination_path)
            finally:
                sftp.close()
            return
        if self.use_wsl:
            destination_dir_q = shlex.quote(parent_dir)
            destination_path_q = shlex.quote(destination_path)
            with open(source_path, "rb") as handle:
                completed = subprocess.run(
                    ["wsl.exe", "-d", self.WSL_DISTRO, "-u", "root", "--", "bash", "-lc", f"mkdir -p {destination_dir_q} && cat > {destination_path_q}"],
                    stdin=handle,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.decode("utf-8", errors="ignore") or "Unable to upload file into WSL")
            return
        os.makedirs(parent_dir, exist_ok=True)
        shutil.copy2(source_path, destination_path)

    def _sftp_mkdir_p(self, sftp, path):
        normalized = path.replace("\\", "/")
        if not normalized or normalized == "/":
            return
        current = ""
        for part in normalized.split("/"):
            if not part:
                current = "/"
                continue
            current = f"{current.rstrip('/')}/{part}" if current else part
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)

    def close(self):
        if self.remote and self.client:
            self.client.close()
