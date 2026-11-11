import json
import re
import shlex
import socket

from .executor import Executor

BENCH_PYTHON_MAP = {
    "version-13": "3.7",
    "version-14": "3.10",
    "version-15": "3.11",
    "version-16": "3.12",
}

BENCH_NODE_MAP = {
    "version-13": "14",
    "version-14": "14",
    "version-15": "18",
    "version-16": "22",
}

BENCH_MARIADB_MIN = {
    "version-13": "10.6.6",
    "version-14": "10.6.6",
    "version-15": "10.6.6",
    "version-16": "11.8.0",
}

UBUNTU_BASIC_PACKAGES = {
    "20.04": "git curl gcc g++ make build-essential pkg-config libffi-dev zlib1g-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses5-dev libncursesw5-dev xz-utils tk-dev libgdbm-dev libnss3-dev libxml2-dev libxmlsec1-dev libdb5.3-dev uuid-dev python3-pip python3-dev wkhtmltopdf redis-server",
    "22.04": "git curl gcc g++ make build-essential pkg-config libffi-dev zlib1g-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses5-dev libncursesw5-dev xz-utils tk-dev libgdbm-dev libnss3-dev libxml2-dev libxmlsec1-dev libdb5.3-dev uuid-dev python3-pip python3-dev python3-testresources wkhtmltopdf redis-server",
    "24.04": "git curl gcc g++ make build-essential pkg-config libffi-dev zlib1g-dev libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses5-dev libncursesw5-dev xz-utils tk-dev libgdbm-dev libnss3-dev libxml2-dev libxmlsec1-dev libdb5.3-dev uuid-dev python3-pip python3-dev python3-setuptools python3-testresources wkhtmltopdf redis-server",
}


def detect_ubuntu_major(executor):
    code, out, err = executor.run("lsb_release -sr || cat /etc/os-release")
    if code != 0:
        return None
    m = re.search(r"(20\.04|22\.04|24\.04)", out)
    return m.group(1) if m else None


def detect_mariadb_version(executor):
    code, out, err = executor.run("mariadb --version || mysql --version", check=False)
    if code != 0:
        return None
    match = re.search(r"Distrib\s+(\d+\.\d+\.\d+)", out)
    if match:
        return match.group(1)
    return None


def version_tuple(value):
    return tuple(int(part) for part in str(value).split(".") if part.isdigit())


def selected_bench_versions(config):
    versions = config.get("bench_versions") or config.get("bench_version") or "version-15"
    if isinstance(versions, str):
        versions = [versions]
    return [str(version).strip() for version in versions if str(version).strip()]


def required_mariadb_version_for_config(config):
    versions = set(selected_bench_versions(config))
    if "version-15" in versions and "version-16" in versions:
        return BENCH_MARIADB_MIN["version-15"]
    if "version-16" in versions:
        return BENCH_MARIADB_MIN["version-16"]
    return BENCH_MARIADB_MIN.get(config.get("bench_version", "version-15"))


def pip_command(ubuntu_version):
    if ubuntu_version == "24.04":
        return "python3 -m pip install --no-cache-dir --progress-bar off --prefer-binary --default-timeout=600 --retries=20 --break-system-packages"
    return "python3 -m pip install --no-cache-dir --progress-bar off --prefer-binary --default-timeout=600 --retries=20"


def package_install_if_missing(packages, apt_wait, env_prefix, apt_install):
    package_list = " ".join(packages)
    checks = " && ".join(f"dpkg -s {pkg} >/dev/null 2>&1" for pkg in packages)
    return (
        f"if {checks}; then "
        f"echo 'Packages already installed: {package_list}'; "
        f"else {apt_wait} && {env_prefix} && sudo -E {apt_install} {package_list}; fi"
    )


def wait_for_dpkg_lock(timeout_seconds=300):
    timeout_seconds = int(timeout_seconds)
    return (
        f'LOCK_WAIT_START="$(date +%s)" && '
        'while true; do '
        'LOCK_PID="$(fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>/dev/null | xargs -r -n1 echo 2>/dev/null | grep -E "^[0-9]+$" | head -n 1 || true)" && '
        'if [ -z "$LOCK_PID" ]; then break; fi; '
        'NOW="$(date +%s)" && '
        f'if [ $((NOW - LOCK_WAIT_START)) -ge {timeout_seconds} ]; then '
        'echo "Timed out waiting for apt/dpkg lock held by pid $LOCK_PID"; exit 1; '
        'fi; '
        'echo "Waiting for apt/dpkg lock held by pid $LOCK_PID..."; '
        'sleep 5; '
        'done'
    )


def command_exists_or_run(binary_name, install_command, message):
    return (
        f"if command -v {binary_name} >/dev/null 2>&1; then "
        f"echo '{message}'; "
        f"else {install_command}; fi"
    )


def command_version_or_run(binary_name, min_major, install_command, ok_message):
    return (
        f"if command -v {binary_name} >/dev/null 2>&1 "
        f"&& [ \"$( {binary_name} -v | sed -E 's/[^0-9]*([0-9]+).*/\\1/' )\" -ge {min_major} ]; then "
        f"echo '{ok_message}'; "
        f"else {install_command}; fi"
    )


def service_stop_if_loaded(service_name, timeout_seconds=20):
    service_name_q = shlex.quote(service_name)
    return (
        f"if systemctl list-unit-files {service_name_q}.service >/dev/null 2>&1 "
        f"|| systemctl list-units --all {service_name_q}.service >/dev/null 2>&1; then "
        f"echo 'Stopping {service_name_q} service...'; "
        f"timeout {int(timeout_seconds)}s sudo systemctl stop {service_name_q} || echo 'Could not stop {service_name_q} cleanly; continuing cleanup'; "
        f"else echo '{service_name_q} service is not loaded'; fi"
    )


def mariadb_service_start_command():
    return (
        "echo 'Starting MariaDB...' "
        "&& ((command -v systemctl >/dev/null 2>&1 && ((sudo systemctl restart mariadb || sudo systemctl start mariadb || sudo systemctl restart mysql || sudo systemctl start mysql) >/dev/null 2>&1)) "
        "|| (command -v service >/dev/null 2>&1 && (sudo service mariadb restart >/dev/null 2>&1 || sudo service mariadb start >/dev/null 2>&1 || sudo service mysql restart >/dev/null 2>&1 || sudo service mysql start >/dev/null 2>&1)) "
        "|| true) "
        "&& if ! (pgrep -x mariadbd >/dev/null 2>&1 || pgrep -x mysqld >/dev/null 2>&1); then "
        "if command -v mysqld_safe >/dev/null 2>&1; then "
        "sudo mkdir -p /var/run/mysqld && (sudo chown mysql:mysql /var/run/mysqld >/dev/null 2>&1 || true) "
        "&& (sudo nohup mysqld_safe --datadir=/var/lib/mysql >/tmp/mysqld-safe.log 2>/tmp/mysqld-safe.err & sleep 5); "
        "fi; "
        "fi "
        "&& ((mariadb-admin ping >/dev/null 2>&1 || mysqladmin ping >/dev/null 2>&1 || pgrep -x mariadbd >/dev/null 2>&1 || pgrep -x mysqld >/dev/null 2>&1) "
        "&& echo 'MariaDB is ready' "
        "|| (echo 'MariaDB did not become ready in time'; exit 1))"
    )


def mariadb_client_binary():
    return '$(if command -v mariadb >/dev/null 2>&1; then printf mariadb; else printf mysql; fi)'


def mariadb_server_binary_exists():
    return "(command -v mariadbd >/dev/null 2>&1 || command -v mysqld >/dev/null 2>&1)"


def mariadb_root_reset_command(password_sql, current_password_cli, target_password_cli):
    db_client = 'db_client="$(command -v mariadb 2>/dev/null || command -v mysql 2>/dev/null)"'
    sql_attempts = [
        f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password_sql}'; FLUSH PRIVILEGES;",
        f"SET PASSWORD FOR 'root'@'localhost' = PASSWORD('{password_sql}'); FLUSH PRIVILEGES;",
        f"ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('{password_sql}'); FLUSH PRIVILEGES;",
    ]

    def run_attempts(prefix):
        attempts = []
        for sql in sql_attempts:
            escaped_sql = sql.replace('"', '\\"')
            attempts.append(f'{prefix} -e "{escaped_sql}"')
        return " || ".join(attempts)

    attempts = [
        run_attempts(f'"$db_client" -uroot -p{target_password_cli}')
    ]
    if current_password_cli and current_password_cli != target_password_cli:
        attempts.append(
            run_attempts(f'"$db_client" -uroot -p{current_password_cli}')
        )
    attempts.append(
        run_attempts('"$db_client"')
    )
    return (
        f"{db_client} && [ -n \"$db_client\" ] "
        '&& echo "Using DB client: $db_client" '
        '&& "$db_client" --version '
        "&& (" + " || ".join(attempts) + ")"
    )


def bench_new_site_command(bench_name, site_name_q, site_admin_password_q, mariadb_root_password_q, current_mariadb_root_password_cli):
    attempts = [
        f"bench new-site {site_name_q} --admin-password {site_admin_password_q} --mariadb-root-password {mariadb_root_password_q} --force",
        f"bench new-site {site_name_q} --admin-password {site_admin_password_q} --mariadb-root-password '' --force",
    ]
    if current_mariadb_root_password_cli:
        attempts.append(
            f"bench new-site {site_name_q} --admin-password {site_admin_password_q} --mariadb-root-password {current_mariadb_root_password_cli} --force"
        )
    return f"cd {bench_name} && (" + " || ".join(attempts) + ")"


def normalize_node_version(node_version):
    return str(node_version).replace(".x", "")


def nodesource_major(node_version):
    normalized = normalize_node_version(node_version)
    return normalized.split(".", 1)[0]


def with_nvm(command, node_version):
    node_version = normalize_node_version(node_version)
    return (
        'export NVM_DIR="${HOME:-/root}/.nvm" && '
        'export PATH="$HOME/.local/bin:$PATH" && '
        'if [ -s "${NVM_DIR}/nvm.sh" ]; then '
        '. "${NVM_DIR}/nvm.sh" && '
        f"nvm install {node_version} >/dev/null && "
        f"nvm use {node_version} >/dev/null && "
        'NVM_NODE_DIR="$(dirname "$(command -v node 2>/dev/null || true)")" && '
        'if [ -n "$NVM_NODE_DIR" ]; then export PATH="$NVM_NODE_DIR:$HOME/.local/bin:$PATH"; fi; '
        "fi && "
        f"{command}"
    )


def bench_binary_resolve_snippet():
    return (
        'BENCH_BIN="$(command -v bench 2>/dev/null || true)" '
        '&& if [ -z "$BENCH_BIN" ] && [ -x "$HOME/.local/bin/bench" ]; then BENCH_BIN="$HOME/.local/bin/bench"; fi '
        '&& if [ -z "$BENCH_BIN" ]; then echo "bench binary not found"; exit 1; fi'
    )


def sql_single_quote(value):
    return str(value).replace("\\", "\\\\").replace("'", "''")


def _rewrite_sudo_commands(cmd, replacement, preserve_user_switch=False):
    pattern = re.compile(r"(^|(?<=[;&|()]))(?P<ws>\s*)sudo\b")

    def replace(match):
        suffix = cmd[match.end():]
        if preserve_user_switch and re.match(r"\s+(?:-\S+\s+)*-u(?:\s|$)", suffix):
            return match.group(0)
        return f"{match.group(1)}{match.group('ws')}{replacement}"

    return pattern.sub(replace, cmd)


def _with_sudo(cmd, sudo_password=None, is_root=False):
    """Convert a command string with sudo to noninteractive usage.

    - root user: keep sudo commands intact so nested user-context commands behave exactly
      like the manual workflow.
    - sudo_password: use SUDO_ASKPASS so piped stdin keeps working.
    - else: sudo -n (fail fast).
    """
    if is_root:
        return cmd

    if sudo_password:
        escaped = shlex.quote(sudo_password)
        askpass_script = (
            "SUDO_ASKPASS_SCRIPT=\"${TMPDIR:-/tmp}/frappe-auto-deploy-askpass-$$.sh\" "
            "&& trap 'rm -f \"$SUDO_ASKPASS_SCRIPT\"' EXIT "
            f"&& printf '%s\\n' '#!/bin/sh' {shlex.quote(f'printf \"%s\\\\n\" {escaped}')} > \"$SUDO_ASKPASS_SCRIPT\" "
            "&& chmod 700 \"$SUDO_ASKPASS_SCRIPT\" "
            "&& export SUDO_ASKPASS=\"$SUDO_ASKPASS_SCRIPT\" "
        )
        sudo_safe_cmd = _rewrite_sudo_commands(cmd, "sudo -A")
        return f"{askpass_script} && {sudo_safe_cmd}"

    return _rewrite_sudo_commands(cmd, "sudo -n")


def run_as_user(command, username):
    username = str(username or "").strip()
    if not username or username == "root":
        return command
    user_q = shlex.quote(username)
    return (
        f"sudo --preserve-env=SUDO_ASKPASS -H -u {user_q} "
        f"bash -lc {shlex.quote('cd \"$HOME\" && ' + command)}"
    )


def run_as_bench_user(command, username, remote=False):
    username = str(username or "").strip()
    if username and username != "__auto_local_user__":
        return run_as_user(command, username)
    if remote:
        return command
    wrapped = shlex.quote('cd "$HOME" && ' + command)
    return (
        'target_user="" && '
        'if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then target_user="${SUDO_USER}"; '
        'else target_user="$(awk -F: \'$3 >= 1000 && $1 != "nobody" {print $1; exit}\' /etc/passwd)"; fi '
        '&& if [ -n "$target_user" ] && [ "$target_user" != "root" ]; then '
        f'sudo --preserve-env=SUDO_ASKPASS -H -u "$target_user" bash -lc {wrapped} '
        f'else {command}; fi'
    )


def mask_sensitive_command(cmd, config):
    masked = cmd
    skip_literal_values = {"root", "admin", ""}
    for key in (
        "sudo_password",
        "ssh_password",
        "ssh_private_key",
        "ssh_key_passphrase",
        "mariadb_root_password",
        "current_mariadb_root_password",
        "site_admin_password",
    ):
        value = str(config.get(key) or "")
        if value and value not in skip_literal_values:
            masked = masked.replace(value, "********")
    return masked


def serialize_recovery_commands(commands):
    return json.dumps(list(commands))


def normalize_recovery_command(command):
    normalized = str(command)
    mariadb_install_marker = "else echo 'Installing MariaDB packages...' && timeout 600s bash -lc "
    if mariadb_install_marker in normalized:
        prefix, rest = normalized.split(mariadb_install_marker, 1)
        if "; fi" in rest:
            quoted_inner, suffix = rest.rsplit("; fi", 1)
            try:
                inner = shlex.split(quoted_inner.strip())[0]
                normalized = f"{prefix}else echo 'Installing MariaDB packages...' && {inner}; fi{suffix}"
            except (IndexError, ValueError):
                pass

    normalized = normalized.replace(
        'sudo POLICY_RC_D="$POLICY_RC_D" -E apt-get',
        'sudo -E env POLICY_RC_D="$POLICY_RC_D" apt-get',
    )
    normalized = re.sub(
        r'POLICY_RC_D="/tmp/frappe-auto-deploy-policy-rc-\$\$" && '
        r"trap '[^']*' EXIT && "
        r"printf '%s\\n' '#!/bin/sh' 'exit 101' > \"\$POLICY_RC_D\" && "
        r'chmod 755 "\$POLICY_RC_D" && ',
        "",
        normalized,
    )
    return re.sub(
        r'(?<!timeout 600s )sudo -E env POLICY_RC_D="\$POLICY_RC_D" apt-get',
        "timeout 600s sudo -E apt-get",
        normalized,
    )


def deserialize_recovery_commands(recovery_commands):
    value = (recovery_commands or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [normalize_recovery_command(command).strip() for command in parsed if str(command).strip()]
        except json.JSONDecodeError:
            pass
    return [normalize_recovery_command(cmd).strip() for cmd in value.splitlines() if cmd.strip()]


def build_commands(config):
    steps = []
    remote = config.get("remote")
    ubuntu_version = config.get("ubuntu_version", "22.04")
    mysql_mode = config.get("mysql_mode", "reset")
    mariadb_root_password = config.get("mariadb_root_password", "root")
    create_swap = config.get("create_swap", False)
    swap_size_gb = int(config.get("swap_size_gb", 4) or 4)
    use_nvm = config.get("use_nvm", True)
    skip_mariadb_root_reset = bool(config.get("skip_mariadb_root_reset"))
    create_site = bool(config.get("create_site", True))
    if not remote and config.get("local_wsl_user"):
        explicit_bench_user = str(config.get("local_wsl_user") or "").strip()
    else:
        explicit_bench_user = str(
            config.get("frappe_user")
            or (config.get("new_username") if config.get("create_user") else "")
            or config.get("ssh_user")
            or ""
        ).strip()
    bench_user = explicit_bench_user or ("__auto_local_user__" if not remote else "")
    production_enabled = bool(remote and config.get("setup_production"))
    ssl_enabled = bool(remote and config.get("generate_ssl") and config.get("domain"))

    def q(value):
        return shlex.quote(str(value))

    mariadb_root_password_sql = sql_single_quote(mariadb_root_password)
    mariadb_root_password_cli = q(mariadb_root_password)
    current_mariadb_root_password = config.get("current_mariadb_root_password", "").strip()
    current_mariadb_root_password_cli = q(current_mariadb_root_password) if current_mariadb_root_password else ""

    if create_swap:
        steps.append((
            "Create swap memory",
            "if swapon --show --noheadings | grep -q .; then "
            "echo 'Swap already enabled'; "
            "else "
            f"(sudo fallocate -l {swap_size_gb}G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1G count={swap_size_gb}) "
            "&& sudo chmod 600 /swapfile "
            "&& sudo mkswap /swapfile "
            "&& sudo swapon /swapfile "
            "&& (grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null) "
            "&& swapon --show; "
            "fi"
        ))

    if config.get("create_user"):
        username = config.get("new_username")
        password = config.get("new_password")
        steps.append((
            "Create user",
            f"if id -u {q(username)} >/dev/null 2>&1; then "
            f"echo 'User already exists. Updating password for {username}'; "
            f"else sudo useradd -m -s /bin/bash {q(username)}; fi "
            f"&& echo {q(f'{username}:{password}')} | sudo chpasswd "
            f"&& sudo usermod -aG sudo {q(username)}"
        ))
    automation_user = str(
        config.get("new_username") if config.get("create_user") else config.get("ssh_user") or ""
    ).strip()
    ssh_public_key = str(config.get("ssh_public_key") or "").strip()
    if remote and automation_user and ssh_public_key:
        automation_home = f"/root" if automation_user == "root" else f"/home/{automation_user}"
        authorized_keys_path = f"{automation_home}/.ssh/authorized_keys"
        steps.append((
            "Install SSH public key",
            f"sudo install -d -m 700 -o {q(automation_user)} -g {q(automation_user)} {q(f'{automation_home}/.ssh')} "
            f"&& sudo touch {q(authorized_keys_path)} "
            f"&& sudo chown {q(f'{automation_user}:{automation_user}')} {q(authorized_keys_path)} "
            f"&& sudo chmod 600 {q(authorized_keys_path)} "
            f"&& if ! sudo grep -qxF -- {q(ssh_public_key)} {q(authorized_keys_path)}; then "
            f"printf '%s\\n' {q(ssh_public_key)} | sudo tee -a {q(authorized_keys_path)} >/dev/null; "
            "fi"
        ))
    if remote and automation_user and automation_user != "root":
        automation_user_q = q(automation_user)
        steps.append((
            "Configure automation sudo",
            f'printf "%s\\n" {q(f"{automation_user} ALL=(ALL) NOPASSWD:ALL")} '
            f'| sudo tee /etc/sudoers.d/99-frappe-auto-deploy-{automation_user_q} >/dev/null '
            f'&& sudo chmod 440 /etc/sudoers.d/99-frappe-auto-deploy-{automation_user_q} '
            f'&& sudo visudo -cf /etc/sudoers.d/99-frappe-auto-deploy-{automation_user_q}'
        ))

    apt_wait = wait_for_dpkg_lock()
    env_prefix = "export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a"
    apt_options = "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o DPkg::Lock::Timeout=300"
    apt_install = f"apt-get {apt_options} install -y"
    apt_upgrade = f"apt-get {apt_options} upgrade -y"
    steps.append(("Repair package state", f"{apt_wait} && {env_prefix} && sudo -E dpkg --configure -a"))
    steps.append((
        "Repair Node.js package conflict",
        f"{apt_wait} && {env_prefix} && "
        "if dpkg -s libnode72 >/dev/null 2>&1 && apt-cache policy nodejs 2>/dev/null | grep -q nodesource; then "
        "echo 'Removing conflicting Ubuntu Node.js 12 packages before upgrade...' "
        f"&& (sudo -E apt-get {apt_options} remove -y nodejs nodejs-doc libnode72 libnode-dev libjs-highlight.js || true) "
        f"&& (sudo -E apt-get {apt_options} autoremove -y || true) "
        "&& sudo -E dpkg --configure -a; "
        "else echo 'No Node.js package conflict detected'; fi"
    ))
    steps.append(("System update", f"{apt_wait} && {env_prefix} && sudo -E apt-get update && sudo -E {apt_upgrade}"))

    basic_packages = UBUNTU_BASIC_PACKAGES.get(ubuntu_version, UBUNTU_BASIC_PACKAGES["22.04"]).split()
    steps.append(("Install basic packages", package_install_if_missing(basic_packages, apt_wait, env_prefix, apt_install)))

    if mysql_mode == "reinstall":
        stop_database_services = (
            service_stop_if_loaded("mariadb")
            + " && "
            + service_stop_if_loaded("mysql")
        )
        steps.append((
            "MariaDB reinstall cleanup",
            f"{apt_wait} && {env_prefix} "
            "&& echo 'Preparing MariaDB cleanup...' "
            f"&& ({stop_database_services}) "
            "&& echo 'Purging MariaDB packages...' "
            f"&& (timeout 180s sudo -E apt-get {apt_options} purge -y mariadb-server mariadb-client mariadb-common || true) "
            "&& echo 'Running autoremove...' "
            f"&& (timeout 180s sudo -E apt-get {apt_options} autoremove -y || true) "
            "&& echo 'Removing MariaDB data directories...' "
            "&& sudo rm -rf /etc/mysql /var/lib/mysql "
            "&& echo 'MariaDB cleanup finished'"
        ))

    mariadb_install_inner = (
        f"{apt_wait} && {env_prefix} "
        f"&& timeout 600s sudo -E {apt_install} mariadb-server mariadb-client "
        "&& echo 'MariaDB packages installed successfully'"
    )
    mariadb_existing_install = (
        "if "
        + mariadb_server_binary_exists()
        + " && command -v mariadb >/dev/null 2>&1; then "
        + "echo 'MariaDB binaries already available on this system'; "
        + f"else echo 'Installing MariaDB packages...' && {mariadb_install_inner}; fi"
    )
    steps.append(("MariaDB install", mariadb_existing_install))

    mariadb_service_start = mariadb_service_start_command()
    mariadb_write_config = (
        "sudo install -d /etc/mysql/mariadb.conf.d "
        "&& printf '%s\\\\n' "
        "'[mysqld]' "
        "'character-set-client-handshake = FALSE' "
        "'character-set-server = utf8mb4' "
        "'collation-server = utf8mb4_unicode_ci' "
        "| sudo tee /etc/mysql/mariadb.conf.d/60-erpnext.cnf >/dev/null"
    )

    if mysql_mode == "keep":
        steps.append(("MariaDB start", mariadb_service_start))
        steps.append(("MariaDB write config", mariadb_write_config))
        steps.append(("MariaDB apply config", mariadb_service_start))
    else:
        steps.append(("MariaDB start", mariadb_service_start))
        if skip_mariadb_root_reset:
            steps.append(("MariaDB root reset", "echo 'Skipping MariaDB root reset for local existing MariaDB setup'"))
        else:
            steps.append((
                "MariaDB root reset",
                mariadb_root_reset_command(
                    mariadb_root_password_sql,
                    current_mariadb_root_password_cli,
                    mariadb_root_password_cli,
                ),
            ))
        steps.append(("MariaDB write config", mariadb_write_config))
        steps.append(("MariaDB apply config", mariadb_service_start))
    steps.append((
        "Install MariaDB build dependencies",
        "if pkg-config --exists mariadb || pkg-config --exists libmariadb || pkg-config --exists mysqlclient; then "
        "echo 'MariaDB client build metadata already available'; "
        f"else {apt_wait} && {env_prefix} && sudo -E {apt_install} pkg-config libmariadb-dev libmariadb-dev-compat; fi"
    ))

    bench_version = config.get("bench_version")
    node_version = normalize_node_version(BENCH_NODE_MAP.get(bench_version, "14"))
    node_source_major = nodesource_major(node_version)
    node_major = int(str(node_version).split(".", 1)[0])
    if bench_user and bench_user != "root" and bench_user != "__auto_local_user__":
        bench_user_q = q(bench_user)
        default_user_home_q = q(f"/home/{bench_user}")
        steps.append((
            "Repair user tool permissions",
            f'id -u {bench_user_q} >/dev/null 2>&1 '
            f'&& USER_HOME="$(getent passwd {bench_user_q} | cut -d: -f6 || true)" '
            f'&& if [ -z "$USER_HOME" ] || [ "$USER_HOME" = "/" ]; then USER_HOME={default_user_home_q}; fi '
            f'&& echo "Repairing tool permissions for {bench_user} in $USER_HOME" '
            f'&& sudo install -d -o {bench_user_q} -g {bench_user_q} "$USER_HOME" "$USER_HOME/.local" "$USER_HOME/.local/bin" "$USER_HOME/.nvm" '
            f'&& sudo chown -R {bench_user_q}:{bench_user_q} "$USER_HOME/.local" "$USER_HOME/.nvm"'
        ))
    if use_nvm:
        steps.append((
            "Node.js setup",
            run_as_bench_user(
                'export NVM_DIR="$HOME/.nvm" && '
                'if [ -d "$NVM_DIR" ] && [ ! -s "$NVM_DIR/nvm.sh" ]; then rm -rf "$NVM_DIR"; fi && '
                'mkdir -p "$NVM_DIR" "$HOME/.local/bin" && '
                'if [ -s "${NVM_DIR}/nvm.sh" ]; then '
                'echo "NVM already installed"; '
                'else curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash; fi '
                '&& if [ -s "${NVM_DIR}/nvm.sh" ]; then '
                '. "${NVM_DIR}/nvm.sh" '
                f'&& nvm install {node_version} '
                f'&& nvm use {node_version} '
                f'&& nvm alias default {node_version} '
                '&& hash -r '
                '&& command -v npm >/dev/null 2>&1 '
                '&& npm install -g yarn '
                '&& echo "Using Node: $(node -v)" '
                '&& echo "Using npm: $(npm -v)" '
                '&& echo "Using yarn: $(yarn -v)"; '
                'else '
                'echo "NVM install failed or nvm.sh is missing"; exit 1; '
                'fi',
                bench_user,
                remote=remote,
            )
        ))
    else:
        node_install = f"curl -fsSL https://deb.nodesource.com/setup_{node_source_major}.x | sudo -E bash - && {apt_wait} && {env_prefix} && sudo -E {apt_install} nodejs"
        steps.append((
            "Node.js setup",
            command_version_or_run("node", node_major, node_install, f"Node.js {node_major}+ already installed")
            + " && "
            + command_exists_or_run("npm", f"{apt_wait} && {env_prefix} && sudo -E {apt_install} npm", "npm already installed")
            + " && "
            + command_exists_or_run("yarn", 'NPM_BIN="$(command -v npm 2>/dev/null || true)" && [ -n "$NPM_BIN" ] && ("$NPM_BIN" install -g yarn || sudo "$NPM_BIN" install -g yarn)', "Yarn already installed")
        ))

    py_version = BENCH_PYTHON_MAP.get(bench_version, "3.10.1")
    py_major_minor = ".".join(str(py_version).split(".")[:2])
    home_resolve_command = (
        'RESOLVED_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"; '
        'if [ -z "$RESOLVED_HOME" ]; then RESOLVED_HOME="$(eval echo "~$(id -un)" 2>/dev/null || true)"; fi; '
        'if [ -z "$RESOLVED_HOME" ] || [ "$RESOLVED_HOME" = "~$(id -un)" ] || [ "$RESOLVED_HOME" = "/" ]; then RESOLVED_HOME="/root"; fi; '
        'HOME="$RESOLVED_HOME"; export HOME; '
        'mkdir -p "$HOME/.local/bin"; '
        'export PATH="$HOME/.local/bin:$PATH"; '
        '[ -n "$HOME" ]'
    )
    uv_discovery_command = (
        'UV_BIN="" && '
        'for CANDIDATE in "$HOME/.local/bin/uv" "$(command -v uv 2>/dev/null || true)"; do '
        'if [ -n "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then UV_BIN="$CANDIDATE"; break; fi; '
        'done'
    )
    uv_bootstrap_command = (
        'echo "uv bootstrap context: USER=$(id -un) HOME=$HOME" && '
        'if [ -z "$HOME" ] || [ "$HOME" = "/" ]; then echo "Refusing unsafe HOME for uv install: $HOME"; exit 1; fi && '
        'if ! command -v curl >/dev/null 2>&1; then echo "curl is required to install uv"; exit 1; fi && '
        'curl -LsSf https://astral.sh/uv/install.sh | sh'
    )
    uv_install_command = (
        f'{home_resolve_command} && '
        'echo "uv install preflight: USER=$(id -un) HOME=$HOME" && '
        f'{uv_discovery_command} && '
        'if [ -n "$UV_BIN" ]; then '
        'echo "uv already installed at $UV_BIN"; '
        'else '
        f'{uv_bootstrap_command} && '
        f'{uv_discovery_command} && '
        'if [ -z "$UV_BIN" ]; then echo "uv installation incomplete"; exit 1; fi; '
        'fi && "$UV_BIN" --version'
    )
    steps.append((
        "Install uv",
        run_as_bench_user(uv_install_command, bench_user, remote=remote),
    ))

    python_resolve_command = (
        f'{home_resolve_command} '
        f'&& {uv_discovery_command} '
        '&& PYTHON_BIN="" '
        '&& if [ -n "$UV_BIN" ]; then '
        f'UV_PYTHON="$("$UV_BIN" python find {py_version} 2>/dev/null || "$UV_BIN" python find {py_major_minor} 2>/dev/null || true)"; '
        'if [ -n "$UV_PYTHON" ] && "$UV_PYTHON" -V >/dev/null 2>&1; then PYTHON_BIN="$UV_PYTHON"; fi; '
        'fi '
        '&& if [ -z "$PYTHON_BIN" ]; then '
        f'for CANDIDATE in "$HOME/.local/share/uv/python/"*/bin/python "$HOME/.local/share/uv/python/"*/bin/python{py_major_minor}; do '
        'if [ -e "$CANDIDATE" ] && "$CANDIDATE" -V >/dev/null 2>&1; then PYTHON_BIN="$CANDIDATE"; break; fi; '
        'done; '
        'fi'
    )
    python_health_check = (
        f"{python_resolve_command} "
        '&& [ -n "$PYTHON_BIN" ] '
        '&& "$PYTHON_BIN" -c "import sqlite3, bz2, lzma, readline, curses" >/dev/null 2>&1'
    )
    python_install_command = (
        f"{python_health_check} "
        f"&& echo 'Python version already available and healthy: {py_version}' "
        f"|| (echo 'Python {py_version} is missing or incomplete. Installing with uv...' "
        f'&& {home_resolve_command} '
        f'&& {uv_discovery_command} '
        '&& if [ -z "$UV_BIN" ]; then '
        f'{uv_bootstrap_command}; '
        'fi '
        f'&& {uv_discovery_command} '
        '&& if [ -z "$UV_BIN" ]; then echo "uv binary not found"; exit 1; fi '
        f'&& "$UV_BIN" python install {py_version} '
        f"&& {python_resolve_command} "
        '&& [ -n "$PYTHON_BIN" ] '
        '&& "$PYTHON_BIN" -c "import sqlite3, bz2, lzma, readline, curses" >/dev/null 2>&1 '
        '&& echo "Installed Python path: $PYTHON_BIN")'
    )
    steps.append((
        "Install Python version",
        run_as_bench_user(python_install_command, bench_user, remote=remote),
    ))

    bench_install_command = (
        f'{python_resolve_command} '
        '&& if command -v bench >/dev/null 2>&1 || [ -x "$HOME/.local/bin/bench" ]; then '
        'echo "Bench already installed"; '
        'else "$UV_BIN" tool install --python "$PYTHON_BIN" frappe-bench; fi '
        '&& export PATH="$HOME/.local/bin:$PATH" '
        '&& command -v bench >/dev/null 2>&1 '
        '&& bench --version'
    )
    steps.append(("Install bench", run_as_bench_user(bench_install_command, bench_user, remote=remote)))

    if production_enabled:
        ansible_install = f"sudo -H {pip_command(ubuntu_version)} ansible"
        steps.append(("Install ansible", command_exists_or_run("ansible", ansible_install, "Ansible already installed")))

    bench_name = str(config.get("bench_name") or f"bench-{bench_version.split('-')[-1]}").strip()
    mysqlclient_env = 'export MYSQLCLIENT_CFLAGS="${MYSQLCLIENT_CFLAGS:--I/usr/include/mariadb}" && export MYSQLCLIENT_LDFLAGS="${MYSQLCLIENT_LDFLAGS:--L/usr/lib/x86_64-linux-gnu -lmariadb}"'
    bench_init_command = (
        'export PATH="$PATH:${HOME:-/root}/.local/bin" '
        '&& echo "Python lookup context: HOME=$HOME USER=$(id -un) SUDO_USER=${SUDO_USER:-}" '
        f'&& {python_resolve_command} '
        '&& if [ -z "$PYTHON_BIN" ]; then echo "Python binary not found"; '
        'if [ -n "$UV_BIN" ]; then '
        f'echo "uv python find {py_version} => $("$UV_BIN" python find {py_version} 2>/dev/null || true)"; '
        'fi; '
        'find "$HOME/.local/share/uv/python" -maxdepth 4 -type f -name "python*" 2>/dev/null | head -n 20 || true; exit 1; fi '
        '&& if ! "$PYTHON_BIN" -c "import sqlite3, bz2, lzma, readline, curses" >/dev/null 2>&1; then '
        'echo "Python binary is present but not healthy"; '
        'exit 1; '
        'fi '
        '&& echo "Using Python: $PYTHON_BIN" '
        '&& BENCH_BIN="$(command -v bench)" '
        '&& if [ -z "$BENCH_BIN" ]; then echo "Bench binary not found. PATH=$PATH HOME=$HOME"; exit 1; fi '
        '&& echo "Using bench: $BENCH_BIN" '
        '&& "$PYTHON_BIN" --version '
        '&& bench --version '
        f'&& {mysqlclient_env} '
        f'&& if ! bench init {bench_name} --frappe-branch {bench_version} --python "$PYTHON_BIN" --no-procfile --no-backups --skip-redis-config-generation; then '
        'echo "bench init command failed"; '
        'exit 1; '
        'fi '
        f'&& if [ ! -d {q(bench_name)}/apps/frappe ]; then '
        'echo "bench init completed but frappe app directory is missing"; '
        'exit 1; '
        'fi'
    )
    get_erpnext_command = (
        f'export PATH="$HOME/.local/bin:$PATH" && cd {bench_name} '
        f'&& bench get-app erpnext --branch {bench_version} --skip-assets'
    )
    bench_setup_redis_command = f'cd {bench_name} && bench setup redis'
    bench_setup_procfile_command = f'cd {bench_name} && bench setup procfile'
    new_site_command = None
    install_app_command = None
    setup_production_command = None

    steps.append((
        "Bench init",
        run_as_bench_user(
            f"if [ -d {q(bench_name)} ]; then "
            f"echo 'Existing bench found. Removing {bench_name} before rebuild...' "
            f"&& rm -rf {q(bench_name)}; "
            f"fi "
            f"&& echo 'Creating fresh bench: {bench_name}...' "
            f"&& {(with_nvm(bench_init_command, node_version) if use_nvm else bench_init_command)}",
            bench_user,
            remote=remote,
        )
    ))
    steps.append((
        "Install ERPNext",
        run_as_bench_user(
            f"cd {bench_name} && if [ -d apps/erpnext ]; then echo 'ERPNext app already present'; "
            f"else {get_erpnext_command.replace(f'cd {bench_name} && ', '')}; fi",
            bench_user,
            remote=remote,
        )
    ))
    if use_nvm:
        bench_setup_redis_command = with_nvm(bench_setup_redis_command, node_version)
        bench_setup_procfile_command = with_nvm(bench_setup_procfile_command, node_version)
    steps.append(("Generate redis config", run_as_bench_user(bench_setup_redis_command, bench_user, remote=remote)))
    steps.append(("Generate Procfile", run_as_bench_user(bench_setup_procfile_command, bench_user, remote=remote)))

    site_name = config.get("site_name")
    site_admin_password = config.get("site_admin_password", "admin")
    if create_site and site_name:
        site_name_q = q(site_name)
        site_admin_password_q = q(site_admin_password)
        mariadb_root_password_q = q(mariadb_root_password)
        new_site_command = bench_new_site_command(
            bench_name,
            site_name_q,
            site_admin_password_q,
            mariadb_root_password_q,
            current_mariadb_root_password_cli,
        )
        install_app_command = f"cd {bench_name} && bench --site {site_name_q} install-app erpnext"
        build_assets_command = (
            f"cd {bench_name} && bench build --app frappe && bench build --app erpnext"
        )
        site_host = config.get("domain") or site_name
        site_host_url = f"https://{site_host}"
        site_config_command = f"cd {bench_name} && bench --site {site_name_q} set-config host_name {q(site_host_url)}"
        dns_multitenant_command = f"cd {bench_name} && bench config dns_multitenant on"
        bench_restart_command = (
            'SUPERVISORCTL="$(command -v supervisorctl 2>/dev/null || true)" && '
            'if [ -z "$SUPERVISORCTL" ] && [ -x "/usr/bin/supervisorctl" ]; then SUPERVISORCTL="/usr/bin/supervisorctl"; fi && '
            'if [ -z "$SUPERVISORCTL" ]; then '
            "echo 'supervisorctl not found; installing supervisor...' && "
            f"{apt_wait} && {env_prefix} && sudo -E {apt_install} supervisor && "
            'SUPERVISORCTL="$(command -v supervisorctl 2>/dev/null || true)" && '
            'if [ -z "$SUPERVISORCTL" ] && [ -x "/usr/bin/supervisorctl" ]; then SUPERVISORCTL="/usr/bin/supervisorctl"; fi; '
            'fi && '
            'if [ -z "$SUPERVISORCTL" ]; then echo "supervisorctl still not found"; exit 1; fi && '
            'sudo "$SUPERVISORCTL" reread && sudo "$SUPERVISORCTL" update && '
            f'SUPERVISOR_BENCH_MATCHES="$(sudo "$SUPERVISORCTL" status 2>/dev/null | grep {q(bench_name)} | awk \'{{print $1}}\' || true)" && '
            'if [ -n "$SUPERVISOR_BENCH_MATCHES" ]; then '
            'echo "Restarting supervisor programs for bench..." && '
            'for proc in $SUPERVISOR_BENCH_MATCHES; do sudo "$SUPERVISORCTL" restart "$proc" || true; done; '
            'else echo "No supervisor programs found for this bench"; fi && '
            f'echo "Supervisor status for {bench_name}:" && (sudo "$SUPERVISORCTL" status | grep {q(bench_name)} || true) && '
            'if command -v nginx >/dev/null 2>&1 || [ -x "/usr/sbin/nginx" ] || systemctl list-unit-files nginx.service >/dev/null 2>&1; then '
            '(sudo systemctl reload nginx || sudo service nginx reload || true); '
            'else echo "nginx not installed; skipping reload"; fi'
        )
        if use_nvm:
            new_site_command = with_nvm(new_site_command, node_version)
            install_app_command = with_nvm(install_app_command, node_version)
            build_assets_command = with_nvm(build_assets_command, node_version)
            site_config_command = with_nvm(site_config_command, node_version)
            dns_multitenant_command = with_nvm(dns_multitenant_command, node_version)
        steps.append((
            "Create ERPNext site",
            run_as_bench_user(
                f"cd {bench_name} && if bench --site {site_name_q} list-apps >/dev/null 2>&1; then echo 'Site already exists: {site_name}'; "
                f"else {new_site_command.replace(f'cd {bench_name} && ', '')}; fi",
                bench_user,
                remote=remote,
            )
        ))
        steps.append((
            "Install ERPNext app",
            run_as_bench_user(
                f"cd {bench_name} && if bench --site {site_name_q} list-apps | grep -q '^erpnext$'; then echo 'ERPNext already installed on site'; "
                f"else {install_app_command.replace(f'cd {bench_name} && ', '')}; fi",
                bench_user,
                remote=remote,
            )
        ))
        steps.append(("Build bench assets", run_as_bench_user(build_assets_command, bench_user, remote=remote)))
        steps.append(("Update site_config.json", run_as_bench_user(site_config_command, bench_user, remote=remote)))
        if remote:
            steps.append(("Enable DNS multitenant", run_as_bench_user(dns_multitenant_command, bench_user, remote=remote)))
        home_permission_user = explicit_bench_user if explicit_bench_user and explicit_bench_user != "__auto_local_user__" else ""
        if home_permission_user and home_permission_user != "root":
            home_permission_user_q = q(home_permission_user)
            steps.append((
                "Fix frappe user home permissions",
                f"if [ -d /home/{home_permission_user_q} ]; then "
                f"sudo chmod -R o+rx /home/{home_permission_user_q}; "
                f"else echo 'Home folder not found for {home_permission_user}'; fi"
            ))
        if production_enabled:
            production_user = explicit_bench_user or (config.get("ssh_user", "root") if remote else config.get("local_wsl_user") or Executor.local_user_fallback())
            nginx_cleanup_command = (
                'if [ -f /etc/nginx/nginx.conf ]; then '
                'echo "Cleaning stale nginx bench includes..." '
                '&& for broken in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/*; do '
                'if [ -L "$broken" ] && [ ! -e "$broken" ]; then sudo rm -f "$broken"; fi; '
                'done '
                '&& for missing in $(grep -oE \'/etc/nginx/(conf\\.d|sites-enabled)/[^ ;]+\' /etc/nginx/nginx.conf 2>/dev/null || true); do '
                'if [ ! -e "$missing" ]; then '
                'ESCAPED_PATH="$(printf "%s\\n" "$missing" | sed \'s/[\\\\/&]/\\\\\\\\&/g\')" '
                '&& sudo sed -i "\\#${ESCAPED_PATH}#d" /etc/nginx/nginx.conf; '
                'fi; '
                'done; '
                'else echo "nginx.conf not present; skipping stale include cleanup"; fi'
            )
            ensure_bench_runtime_command = f"cd {bench_name} && bench setup redis && bench setup procfile"
            if use_nvm:
                ensure_bench_runtime_command = with_nvm(ensure_bench_runtime_command, node_version)
            steps.append(("Cleanup stale nginx includes", run_as_bench_user(nginx_cleanup_command, bench_user, remote=remote)))
            steps.append(("Ensure bench runtime config", run_as_bench_user(ensure_bench_runtime_command, bench_user, remote=remote)))
            setup_production_command = (
                f"{nginx_cleanup_command} && "
                f"{ensure_bench_runtime_command} && "
                f"{bench_binary_resolve_snippet()} && "
                f"if ls /etc/supervisor/conf.d/*{bench_name}*.conf >/dev/null 2>&1 "
                f"|| ls /etc/nginx/conf.d/*{bench_name}*.conf >/dev/null 2>&1 "
                f"|| ls /etc/nginx/sites-enabled/*{bench_name}* >/dev/null 2>&1; then "
                f"echo 'Production setup already exists for {bench_name}'; "
                f"else cd {bench_name} && yes y | sudo \"$BENCH_BIN\" setup production {q(production_user)}; fi"
            )
            if use_nvm:
                setup_production_command = with_nvm(setup_production_command, node_version)
            steps.append(("Setup production", run_as_bench_user(setup_production_command, bench_user, remote=remote)))
            rerun_production_command = (
                f"{nginx_cleanup_command} && "
                f"{ensure_bench_runtime_command} && "
                f"{bench_binary_resolve_snippet()} && "
                f"cd {bench_name} && yes y | sudo \"$BENCH_BIN\" setup production {q(production_user)}"
            )
            if use_nvm:
                rerun_production_command = with_nvm(rerun_production_command, node_version)
            steps.append(("Re-run production setup", run_as_bench_user(rerun_production_command, bench_user, remote=remote)))
            steps.append(("Restart bench", bench_restart_command))

    if ssl_enabled:
        domain = config.get("domain")
        domain_q = q(domain)
        email = q(f"admin@{domain}")
        cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"
        cert_path_q = q(cert_path)
        key_path_q = q(key_path)
        steps.append((
            "Install Certbot",
            command_exists_or_run(
                "certbot",
                f"{apt_wait} && {env_prefix} && sudo -E {apt_install} certbot python3-certbot-nginx",
                "Certbot already installed",
            ),
        ))
        steps.append((
            "Generate SSL",
            f"if [ -f {cert_path_q} ] && [ -f {key_path_q} ]; then "
            f"echo 'SSL certificate already exists for {domain}'; "
            f"else sudo certbot --nginx -d {domain_q} --non-interactive --agree-tos -m {email}; fi"
        ))
        if site_name:
            steps.append((
                "Store SSL paths in site_config.json",
                f"if [ -f {cert_path_q} ] && [ -f {key_path_q} ]; then "
                f"cd {bench_name} && bench --site {site_name_q} set-config ssl_certificate {cert_path_q} "
                f"&& bench --site {site_name_q} set-config ssl_certificate_key {key_path_q}; "
                "else echo 'Certificate files are missing after Certbot run.'; exit 1; fi"
            ))
        steps.append(("Show cert path", f"sudo ls -al /etc/letsencrypt/live/{domain_q}"))

    sudo_password = config.get("sudo_password") or config.get("ssh_password")
    access_mode = config.get("access_mode", "root")
    is_root_user = access_mode == "root" or str(config.get("ssh_user", "root")).strip() == "root"

    final_steps = [
        (title, _with_sudo(cmd, sudo_password=sudo_password, is_root=is_root_user))
        for title, cmd in steps
    ]

    return final_steps


def execute_deployment(config, log_callback=None, should_pause=None):
    remote = config.get("remote")
    use_wsl = config.get("use_wsl", False)
    try:
        executor = Executor(
            remote=remote,
            host=config.get("server_ip"),
            port=config.get("ssh_port", 22),
            username=config.get("ssh_user"),
            password=config.get("ssh_password"),
            use_wsl=use_wsl,
            use_ssh_key=config.get("use_ssh_key", False),
            ssh_private_key=config.get("ssh_private_key", ""),
            ssh_key_passphrase=config.get("ssh_key_passphrase", ""),
        )
    except Exception as exc:
        if log_callback:
            port = config.get("ssh_port", 22)
            host = config.get("server_ip", "")
            message = str(exc)
            if host and port:
                log_callback(
                    f"Connection issue: could not connect to `{host}:{port}`. "
                    "Check the server IP, SSH port, firewall rules, and whether the server is online."
                )
            log_callback(f"Technical details: {message}")
        return False, None

    if use_wsl:
        if log_callback:
            log_callback(
                f"Local deployment mode is active. Commands will run inside WSL distro `{Executor.WSL_DISTRO}`, not on Windows directly."
            )
        has_distro, distro_error = executor.has_wsl_distro()
        if not has_distro:
            if log_callback:
                log_callback("Windows local mode needs WSL Ubuntu 22.04, but it is not installed yet.")
            install_result = executor.install_wsl_distro()
            if log_callback:
                if install_result.stdout.strip():
                    log_callback(install_result.stdout.strip())
                if install_result.stderr.strip():
                    log_callback(install_result.stderr.strip())
            if install_result.returncode != 0:
                executor.close()
                return False, None
            if log_callback:
                log_callback("WSL install command finished. If Windows asks for a restart, restart Windows, then reopen the app and run again.")
                log_callback("Stopping here so setup does not continue against an uninitialized WSL distro.")
            executor.close()
            return False, None
        local_wsl_user = executor.detect_wsl_default_user()
        if local_wsl_user and local_wsl_user != "root":
            config["local_wsl_user"] = local_wsl_user
            if log_callback:
                log_callback(
                    f"Detected WSL default user: `{local_wsl_user}`. User-owned steps like uv, Python install, NVM, and bench init will run in that home directory."
                )
        elif log_callback:
            log_callback(
                "Could not detect a non-root default WSL user. Local user-owned steps will fall back to auto-detection inside the distro."
            )

    ubuntu_version = detect_ubuntu_major(executor)
    if ubuntu_version:
        config["ubuntu_version"] = ubuntu_version
        if log_callback:
            log_callback(f"Detected Ubuntu: {ubuntu_version}")
    else:
        if log_callback:
            log_callback("Could not detect the Ubuntu version. Default package settings will be used.")

    bench_version = config.get("bench_version", "version-15")
    mysql_mode = config.get("mysql_mode", "reset")
    mariadb_version = detect_mariadb_version(executor)
    required_mariadb_version = required_mariadb_version_for_config(config)
    if mariadb_version and log_callback:
        log_callback(f"Detected MariaDB: {mariadb_version}")
    if log_callback:
        selected_versions = ", ".join(selected_bench_versions(config))
        log_callback(
            f"MariaDB compatibility target: {required_mariadb_version}+ for {selected_versions}."
        )
    if mysql_mode != "reinstall" and mariadb_version and required_mariadb_version:
        if version_tuple(mariadb_version) < version_tuple(required_mariadb_version):
            if log_callback:
                log_callback(
                    f"[WARNING] MariaDB version mismatch: selected bench version(s) prefer MariaDB {required_mariadb_version}+ "
                    f"but the server currently has {mariadb_version}."
                )
                log_callback(
                    "MySQL keep/reset mode is active, so the current database server will be reused as-is."
                )
                log_callback(
                    "Deployment will continue with the current MariaDB version. If a database compatibility issue appears later, switch to a supported version or reinstall MariaDB."
                )

    if mysql_mode == "reinstall" and mariadb_version and required_mariadb_version:
        if version_tuple(mariadb_version) >= version_tuple(required_mariadb_version):
            config["mysql_mode"] = "reset"
            mysql_mode = "reset"
            if log_callback:
                location_label = "WSL local deployment" if use_wsl and not remote else "Deployment target"
                log_callback(
                    f"{location_label} already has compatible MariaDB {mariadb_version}. "
                    "Switching MySQL action from reinstall to reset to avoid unnecessary MariaDB purge/reinstall cleanup."
                )
    if use_wsl and not remote and mariadb_version:
        config["skip_mariadb_root_reset"] = True
        if log_callback:
            log_callback(
                "Local WSL deployment will reuse the current MariaDB root auth instead of forcing a root password reset."
            )
    if remote:
        network_checks = [
            "getent hosts github.com",
            "getent hosts deb.nodesource.com",
            "ping -c 1 8.8.8.8",
        ]
        network_ok = False
        for command in network_checks:
            code, _, _ = executor.run(command, check=False)
            if code == 0:
                network_ok = True
                break
        if not network_ok:
            if log_callback:
                log_callback(
                    "Network issue detected on the server. The machine cannot resolve package hosts or reach the internet."
                )
                log_callback(
                    "Check DNS, outbound firewall rules, cloud security groups, and whether `apt`/`curl` can access the internet."
                )
            executor.close()
            return False, None

    ssh_user = str(config.get("ssh_user", "root")).strip()
    sudo_password = config.get("sudo_password")
    access_mode = config.get("access_mode", "root")
    if access_mode == "user" and not sudo_password and not use_wsl:
        if log_callback:
            log_callback(
                "[ERROR] User access mode requires a sudo password or passwordless sudo.\n"
                "Fill the Sudo Password field or configure NOPASSWD for this user."
            )
        executor.close()
        return False, None

    commands = build_commands(config)
    total = len(commands)

    if log_callback:
        log_callback(
            {
                "type": "plan",
                "total": total,
                "steps": [title for title, _ in commands],
            }
        )
        log_callback("[PLAN] Deployment steps:")
        for idx, (title, _) in enumerate(commands, start=1):
            log_callback(f"  • {idx}. {title}")

    recovery_commands = []
    for index, (title, cmd) in enumerate(commands, start=1):
        percent = int((index - 1) / total * 100)
        recovery_commands.append(cmd)
        display_cmd = mask_sensitive_command(cmd, config)
        if log_callback:
            log_callback({"type": "progress", "value": percent})
            log_callback(
                {
                    "type": "current_step",
                    "title": title,
                    "command": display_cmd,
                    "index": index,
                    "total": total,
                }
            )
            log_callback(
                {
                    "type": "step_started",
                    "title": title,
                    "command": display_cmd,
                    "index": index,
                    "total": total,
                }
            )
            log_callback(f"  • {index}/{total}: {title}")
            log_callback(f"[START] {title}: {display_cmd}")

        def on_stream(stream, content):
            if log_callback and content:
                log_callback(
                    {
                        "type": "step_output",
                        "stream": stream,
                        "title": title,
                        "index": index,
                        "content": content,
                    }
                )

        code, out, err = executor.run(cmd, stream_callback=on_stream)
        stdout_text = out.strip()
        stderr_text = err.strip()

        if log_callback:
            if not stdout_text and not stderr_text:
                log_callback("[OUT] <no output>")

        if code != 0:
            failure_details = stderr_text or stdout_text or f"Command failed with exit code {code}."
            if log_callback:
                log_callback({"type": "progress", "value": 0})
                log_callback(f"[ERROR] {title} failed with code {code}. Aborting.")
                log_callback(
                    {
                        "type": "failed_step",
                        "title": title,
                        "command": display_cmd,
                        "code": code,
                        "error": failure_details,
                        "index": index,
                        "total": total,
                    }
                )
            executor.close()
            return False, serialize_recovery_commands(remaining_cmd for _, remaining_cmd in commands[index - 1 :])

        if log_callback:
            log_callback(f"[DONE] {title}")
            log_callback(
                {
                    "type": "step_finished",
                    "title": title,
                    "index": index,
                    "total": total,
                    "status": "done",
                }
            )
            percent_done = int(index / total * 100)
            log_callback({"type": "progress", "value": percent_done})

        if should_pause and should_pause() and index < total:
            remaining = serialize_recovery_commands(remaining_cmd for _, remaining_cmd in commands[index:])
            if log_callback:
                log_callback("[PAUSED] Deployment paused after the current step.")
            executor.close()
            return False, remaining

    executor.close()
    return True, None


def execute_resume_deployment(config, recovery_commands, log_callback=None, should_pause=None):
    commands = [
        (f"Resume step {index}", cmd.strip())
        for index, cmd in enumerate(deserialize_recovery_commands(recovery_commands), start=1)
        if cmd.strip()
    ]
    if not commands:
        return False, None

    try:
        executor = Executor(
            remote=config.get("remote"),
            host=config.get("server_ip"),
            port=config.get("ssh_port", 22),
            username=config.get("ssh_user"),
            password=config.get("ssh_password"),
            use_wsl=config.get("use_wsl", False),
            use_ssh_key=config.get("use_ssh_key", False),
            ssh_private_key=config.get("ssh_private_key", ""),
            ssh_key_passphrase=config.get("ssh_key_passphrase", ""),
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[ERROR] Resume connection failed: {exc}")
        return False, recovery_commands

    total = len(commands)
    if log_callback:
        log_callback(
            {
                "type": "plan",
                "total": total,
                "steps": [title for title, _ in commands],
            }
        )

    for index, (title, cmd) in enumerate(commands, start=1):
        display_cmd = mask_sensitive_command(cmd, config)
        if log_callback:
            log_callback({"type": "progress", "value": int((index - 1) / total * 100)})
            log_callback({"type": "current_step", "title": title, "command": display_cmd, "index": index, "total": total})
            log_callback({"type": "step_started", "title": title, "command": display_cmd, "index": index, "total": total})

        def on_stream(stream, content):
            if log_callback and content:
                log_callback({"type": "step_output", "stream": stream, "title": title, "index": index, "content": content})

        code, out, err = executor.run(cmd, stream_callback=on_stream)
        stdout_text = out.strip()
        stderr_text = err.strip()
        if code != 0:
            failure_details = stderr_text or stdout_text or f"Command failed with exit code {code}."
            if log_callback:
                log_callback({"type": "failed_step", "title": title, "command": display_cmd, "code": code, "error": failure_details, "index": index, "total": total})
            executor.close()
            return False, serialize_recovery_commands(remaining_cmd for _, remaining_cmd in commands[index - 1 :])
        if log_callback:
            log_callback({"type": "step_finished", "title": title, "index": index, "total": total, "status": "done"})
            log_callback({"type": "progress", "value": int(index / total * 100)})

        if should_pause and should_pause() and index < total:
            remaining = serialize_recovery_commands(remaining_cmd for _, remaining_cmd in commands[index:])
            if log_callback:
                log_callback("[PAUSED] Deployment paused after the current resume step.")
            executor.close()
            return False, remaining

    executor.close()
    return True, None
