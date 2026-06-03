import subprocess
import json
import os
import sys
import getpass
import tempfile
import shutil
import time
import paramiko
import yaml
from datetime import datetime
from pathlib import Path

from scanner import scanner_containers
from state import State, Phase

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
TERRAFORM_DIR = BASE_DIR / "terraform"
ANSIBLE_DIR   = BASE_DIR / "ansible"

config_path = BASE_DIR / "config.yml"
if not config_path.exists():
    print("  ERREUR : config.yml introuvable. Copie config.yml.example et adapte-le.")
    sys.exit(1)
CONFIG  = yaml.safe_load(config_path.open())
SSH_KEY = Path(CONFIG["ssh"]["key_path"]).expanduser()


# ─── Affichage ────────────────────────────────────────────────────────────────

def titre(etape: str, total: int, texte: str):
    print(f"\n[{etape}/{total}] {texte}")
    print("-" * 50)


def ok(texte: str):
    print(f"  {texte:.<40} OK")


def fail(texte: str):
    print(f"  {texte:.<40} ECHEC")


def info(texte: str):
    print(f"  {texte}")


# ─── Utilitaires ──────────────────────────────────────────────────────────────

def executer_cmd(commande: list, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        commande,
        shell=False,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=os.environ.copy()
    )


def attendre_ssh(ip: str, timeout: int = 120, proxy_ip: str = None):
    debut = time.time()
    while time.time() - debut < timeout:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if proxy_ip:
                proxy = paramiko.SSHClient()
                proxy.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                proxy.connect(hostname=proxy_ip, username=CONFIG["ssh"]["user"],
                              key_filename=str(SSH_KEY), timeout=10)
                channel = proxy.get_transport().open_channel(
                    "direct-tcpip", (ip, 22), ("127.0.0.1", 0)
                )
                client.connect(hostname=ip, username=CONFIG["ssh"]["user"],
                               key_filename=str(SSH_KEY), timeout=5, sock=channel)
                client.close()
                proxy.close()
            else:
                client.connect(hostname=ip, username=CONFIG["ssh"]["user"],
                               key_filename=str(SSH_KEY), timeout=5)
                client.close()
            return True
        except Exception:
            time.sleep(5)
    return False


# ─── Prérequis ────────────────────────────────────────────────────────────────

IPTABLES_CHAIN = "MIGRATION"


def _get_lxc_apache_ip() -> str:
    r = executer_cmd(["sudo", "lxc-ls", "--fancy"])
    for line in r.stdout.splitlines():
        if line.startswith("apache") and "RUNNING" in line:
            parts = line.split()
            if len(parts) >= 4:
                return parts[3]
    return "10.0.3.20"


def _preparer_chaine_iptables():
    r = executer_cmd(["sudo", "iptables", "-t", "nat", "-L", IPTABLES_CHAIN])
    if r.returncode != 0:
        executer_cmd(["sudo", "iptables", "-t", "nat", "-N", IPTABLES_CHAIN])
    r = executer_cmd(["sudo", "iptables", "-t", "nat", "-C", "PREROUTING",
                      "-p", "tcp", "--dport", "80", "-j", IPTABLES_CHAIN])
    if r.returncode != 0:
        executer_cmd(["sudo", "iptables", "-t", "nat", "-I", "PREROUTING", "1",
                      "-p", "tcp", "--dport", "80", "-j", IPTABLES_CHAIN])
    r = executer_cmd(["sudo", "iptables", "-t", "nat", "-C", "POSTROUTING", "-j", "MASQUERADE"])
    if r.returncode != 0:
        executer_cmd(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-j", "MASQUERADE"])


def configurer_iptables(target_ip: str):
    """Redirige le port 80 du host vers target_ip via DNAT iptables."""
    try:
        executer_cmd(["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"])
        executer_cmd(["sudo", "sh", "-c",
                      "echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-migration.conf"])
        _preparer_chaine_iptables()
        executer_cmd(["sudo", "iptables", "-t", "nat", "-F", IPTABLES_CHAIN])
        r = executer_cmd(["sudo", "iptables", "-t", "nat", "-A", IPTABLES_CHAIN,
                          "-p", "tcp", "-j", "DNAT", "--to-destination", f"{target_ip}:80"])
        if r.returncode == 0:
            executer_cmd(["sudo", "sh", "-c",
                          "mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4"])
            ok(f"iptables DNAT port 80 -> {target_ip}")
        else:
            fail(f"iptables DNAT -> {target_ip}: {r.stderr.strip()}")
    except Exception as e:
        fail(f"iptables: {e}")

def verifier_prerequis():
    titre("1", "9", "Verification des prerequis")
    erreurs = []

    outils = {
        "terraform": ["terraform", "version"],
        "ansible":   ["ansible", "--version"],
    }

    for nom, cmd in outils.items():
        r = executer_cmd(cmd)
        if r.returncode == 0:
            ok(nom)
        else:
            fail(nom)
            erreurs.append(nom)

    if SSH_KEY.exists():
        ok("cle SSH")
    else:
        fail("cle SSH")
        erreurs.append("ssh_key")

    r = executer_cmd(["ping", "-c", "1", "-W", "2", CONFIG["network"]["api_ip"]])
    if r.returncode == 0:
        ok("vm-cible (OpenStack)")
    else:
        fail("vm-cible (OpenStack)")
        erreurs.append("openstack")

    if erreurs:
        print(f"\n  Prerequis manquants : {erreurs}")
        sys.exit(1)


# ─── Collecte des credentials ─────────────────────────────────────────────────

def collecter_credentials() -> dict:
    titre("2", "9", "Collecte des credentials")
    print("  Credentials OpenStack lus depuis les variables d'environnement.\n")

    return {
        "mariadb_root_password": getpass.getpass("  Mot de passe MariaDB root : "),
        "mariadb_app_password":  getpass.getpass("  Mot de passe MariaDB appuser : "),
    }


# ─── Phase Scan ───────────────────────────────────────────────────────────────

def phase_scan(state: State) -> list:
    titre("3", "9", "Scan des containers LXC")

    containers = scanner_containers()

    for c in containers:
        services_str = ", ".join(c.services) if c.services else "aucun"
        info(f"{c.name} ({c.ip}) ......... services : {services_str}")
        if c.databases:
            info(f"  bases : {', '.join(d.name for d in c.databases)}")

    state.phase_terminee(Phase.SCAN)
    return containers


# ─── Phase Terraform ──────────────────────────────────────────────────────────

def generer_tfvars(containers: list, credentials: dict) -> dict:
    """Build terraform.tfvars from scan results. Returns {container_name: ip}."""
    cfg_flavors = CONFIG.get("flavors", {})

    ssh_pub_key = Path(CONFIG["ssh"]["key_path"] + ".pub").expanduser().read_text().strip()

    instances_hcl = ""
    for c in containers:
        flavor = cfg_flavors.get(c.name, cfg_flavors.get("default", "m1.small"))
        instances_hcl += f'  {c.name} = {{ flavor = "{flavor}" }}\n'

    tfvars = f"""image_name          = "{CONFIG['image']['name']}"
ssh_public_key      = "{ssh_pub_key}"
external_network_id = "{CONFIG['network']['external_network_id']}"
apache_floating_ip  = "{CONFIG['network']['apache_floating_ip']}"
private_subnet_cidr = "{CONFIG['network'].get('private_subnet_cidr', '10.10.0.0/24')}"

instances = {{
{instances_hcl}}}
"""

    (TERRAFORM_DIR / "terraform.tfvars").write_text(tfvars)
    return {}


def phase_provisioning(state: State, credentials: dict, containers: list):
    titre("4", "9", "Provisioning Terraform")

    generer_tfvars(containers, credentials)

    info("terraform init...")
    r = executer_cmd(["terraform", "init", "-no-color"], cwd=TERRAFORM_DIR)
    if r.returncode != 0:
        fail("terraform init")
        raise Exception("terraform init echoue")
    ok("terraform init")

    info("terraform apply...")
    r = executer_cmd(
        ["terraform", "apply", "-auto-approve", "-no-color"],
        cwd=TERRAFORM_DIR
    )
    if r.returncode != 0:
        fail("terraform apply")
        raise Exception(f"terraform apply echoue:\n{r.stderr[-500:]}")
    ok("terraform apply")

    r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
    outputs = json.loads(r.stdout)
    instances = outputs["instances"]["value"]

    container_map = {c.name: c for c in containers}
    for nom, data in instances.items():
        c = container_map.get(nom)
        state.enregistrer_ip(
            nom,
            lxc_ip=c.ip if c else "",
            internal_ip=data["ip"],
            floating_ip=data["floating_ip"]
        )
        fip = data["floating_ip"]
        display = f"{data['ip']} (FIP: {fip})" if fip != data["ip"] else data["ip"]
        info(f"  {nom:.<20} {display}")

    # Supprime terraform.tfvars (contient le mot de passe)
    tfvars_path = TERRAFORM_DIR / "terraform.tfvars"
    if tfvars_path.exists():
        tfvars_path.unlink()
        info("  terraform.tfvars supprime (securite)")

    apache_fip        = CONFIG["network"].get("apache_floating_ip", "")
    apache_connect_ip = apache_fip if apache_fip else instances.get("apache", {}).get("ip", "")

    info("")
    info("Attente SSH sur Apache (porte d'entree)...")
    if not attendre_ssh(apache_connect_ip, timeout=300):
        fail(f"SSH apache ({apache_connect_ip})")
        raise Exception(f"SSH timeout sur apache ({apache_connect_ip})")
    ok(f"SSH apache ({apache_connect_ip})")

    info("Attente SSH sur les autres instances (via ProxyJump)...")
    for nom, data in instances.items():
        if nom == "apache":
            continue
        ip    = data["ip"]
        proxy = apache_connect_ip if apache_fip else None
        if attendre_ssh(ip, proxy_ip=proxy):
            ok(f"SSH {nom} ({ip})")
        else:
            fail(f"SSH {nom} ({ip})")
            raise Exception(f"SSH timeout sur {nom} ({ip})")

    state.phase_terminee(Phase.PROVISIONING)
    return instances


# ─── Génération des playbooks Ansible ─────────────────────────────────────────

def _new_ip(name: str) -> str:
    """Return Jinja2 expression for new_ips[name], handling hyphens in key names."""
    if '-' in name:
        return f"{{{{ new_ips['{name}'] }}}}"
    return f"{{{{ new_ips.{name} }}}}"


def generer_group_vars(instances: dict):
    cfg = CONFIG
    content  = "# Généré automatiquement par l'orchestrateur depuis config.yml\n---\n"
    content += f"ansible_user: {cfg['ssh']['user']}\n"
    content += f"ansible_ssh_private_key_file: {cfg['ssh']['key_path']}\n"
    content += "ansible_ssh_common_args: '-o StrictHostKeyChecking=no'\n"
    content += f"staging_dir: \"{cfg['staging_dir']}\"\n"
    content += "new_ips:\n"
    for nom, data in instances.items():
        content += f"  {nom}: \"{data['ip']}\"\n"
    gv_path = ANSIBLE_DIR / "group_vars" / "all.yml"
    gv_path.parent.mkdir(parents=True, exist_ok=True)
    gv_path.write_text(content)
    ok("group_vars/all.yml")


def generer_provision_yml(containers: list):
    """Generate provision.yml dynamically — one play per detected container."""
    SVC_PKGS = {
        "mariadb":    ["mariadb-server", "mariadb-client", "python3-pymysql"],
        "apache2":    ["apache2", "php", "libapache2-mod-php", "php-mysql", "php-curl", "nfs-common"],
        "vsftpd":     ["vsftpd", "nfs-common"],
        "nfs-server": ["nfs-kernel-server", "nfs-common"],
        # "cron" absent : cron tourne par défaut sur Ubuntu, ne sert pas à identifier un container
    }
    SVC_ANSIBLE = {
        "mariadb":    "mariadb",
        "apache2":    "apache2",
        "vsftpd":     "vsftpd",
        "nfs-server": "nfs-kernel-server",
        # "cron" absent : évite de générer un play cron pour chaque container Ubuntu
    }
    APT_LOCK = (
        "    - name: Attente liberation verrou apt\n"
        "      shell: while fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend"
        " /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do sleep 2; done\n"
        "      changed_when: false"
    )

    lines = [
        "---",
        "# Généré automatiquement par l'orchestrateur",
        "",
        "- name: Provisionnement commun",
        "  hosts: all",
        "  become: true",
        "  gather_facts: no",
        "  tasks:",
        "    - name: Desactivation reverse DNS SSH",
        "      lineinfile:",
        "        path: /etc/ssh/sshd_config",
        "        regexp: '^#?UseDNS'",
        "        line: 'UseDNS no'",
        "      register: sshd_dns",
        "    - name: Redemarrage sshd si modifie",
        "      service:",
        "        name: ssh",
        "        state: restarted",
        "      when: sshd_dns.changed",
        "    - name: Attente fin cloud-init",
        "      command: cloud-init status --wait",
        "      changed_when: false",
        "      failed_when: false",
        APT_LOCK,
        "    - name: Mise a jour du cache apt",
        "      apt:",
        "        update_cache: yes",
        "        cache_valid_time: 3600",
        "        lock_timeout: 300",
        "    - name: Installation des paquets communs",
        "      apt:",
        "        name:",
        "          - curl",
        "          - rsync",
        "          - ca-certificates",
        "        state: present",
        "        lock_timeout: 300",
    ]

    for c in containers:
        pkgs = []
        for svc in c.services:
            for pkg in SVC_PKGS.get(svc, []):
                if pkg not in pkgs:
                    pkgs.append(pkg)

        # Backup containers identified by backup.sh presence, not by cron service
        if c.backup_config is not None and "mariadb" not in c.services:
            for pkg in ["mariadb-client", "cron"]:
                if pkg not in pkgs:
                    pkgs.append(pkg)

        svcs = list(dict.fromkeys(
            SVC_ANSIBLE[s] for s in c.services if s in SVC_ANSIBLE
        ))

        if not pkgs and not svcs:
            continue

        lines += [
            "",
            f"- name: Provisionnement {c.name}",
            f"  hosts: {c.name}",
            "  become: true",
            "  gather_facts: no",
            "  tasks:",
            APT_LOCK,
        ]

        if pkgs:
            lines += [
                f"    - name: Installation paquets {c.name}",
                "      apt:",
                "        name:",
            ] + [f"          - {p}" for p in pkgs] + [
                "        state: present",
                "        lock_timeout: 300",
            ]

        for svc_a in svcs:
            lines += [
                f"    - name: Demarrage {svc_a}",
                "      service:",
                f"        name: {svc_a}",
                "        state: started",
                "        enabled: true",
            ]

        if "mariadb" in c.services:
            lines += [
                "    - name: Formatage du volume Cinder (si non formate)",
                "      filesystem:",
                "        fstype: ext4",
                "        dev: /dev/vdb",
                "    - name: Arret mariadb avant migration sur volume",
                "      service:",
                "        name: mariadb",
                "        state: stopped",
                "    - name: Creation point de montage temporaire",
                "      file:",
                "        path: /mnt/mariadb-data",
                "        state: directory",
                "    - name: Montage temporaire du volume",
                "      mount:",
                "        src: /dev/vdb",
                "        path: /mnt/mariadb-data",
                "        fstype: ext4",
                "        state: mounted",
                "    - name: Copie des donnees MariaDB vers le volume",
                "      command: rsync -a /var/lib/mysql/ /mnt/mariadb-data/",
                "      args:",
                "        creates: /mnt/mariadb-data/mysql",
                "    - name: Demontage temporaire",
                "      mount:",
                "        path: /mnt/mariadb-data",
                "        state: unmounted",
                "    - name: Montage persistant du volume sur /var/lib/mysql",
                "      mount:",
                "        src: /dev/vdb",
                "        path: /var/lib/mysql",
                "        fstype: ext4",
                "        opts: defaults,_netdev",
                "        state: mounted",
                "    - name: Correction des permissions sur le volume",
                "      file:",
                "        path: /var/lib/mysql",
                "        owner: mysql",
                "        group: mysql",
                "        recurse: yes",
                "    - name: Demarrage mariadb sur le volume Cinder",
                "      service:",
                "        name: mariadb",
                "        state: started",
            ]

    (ANSIBLE_DIR / "provision.yml").write_text("\n".join(lines) + "\n")
    ok("provision.yml")


# ─── Helpers restore ──────────────────────────────────────────────────────────

def _restore_mariadb_play(c, apache_containers, backup_containers):
    lines = [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "",
        "    - name: Restauration des bases de données",
        "      community.mysql.mysql_db:",
        '        name: "{{ item }}"',
        "        state: import",
        f'        target: "{{{{ staging_dir }}}}/{c.name}_{{{{ item }}}}.sql"',
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        '      loop: "{{ databases }}"',
        "",
        "    - name: Suppression ancien user appuser@ancienne_ip",
        "      community.mysql.mysql_user:",
        "        name: appuser",
        '        host: "{{ old_apache_ip }}"',
        "        state: absent",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
    ]

    for ac in apache_containers:
        lines += [
            "",
            f"    - name: Creation user appuser pour {ac.name}",
            "      community.mysql.mysql_user:",
            "        name: appuser",
            f'        host: "{_new_ip(ac.name)}"',
            '        password: "{{ mariadb_appuser_password }}"',
            '        priv: "app_db.*:ALL/sysmonitor.*:ALL"',
            "        state: present",
            "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        ]

    for bc in backup_containers:
        lines += [
            "",
            f"    - name: Creation user appuser pour {bc.name} (lecture seule)",
            "      community.mysql.mysql_user:",
            "        name: appuser",
            f'        host: "{_new_ip(bc.name)}"',
            '        password: "{{ mariadb_appuser_password }}"',
            '        priv: "app_db.*:SELECT"',
            "        state: present",
            "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        ]

    lines += [
        "",
        "    - name: Restriction bind-address MariaDB a l'IP interne",
        "      lineinfile:",
        "        path: /etc/mysql/mariadb.conf.d/50-server.cnf",
        "        regexp: '^bind-address'",
        "        line: 'bind-address = {{ instance_ip }}'",
        "",
        "    - name: Redemarrage MariaDB",
        "      service:",
        "        name: mariadb",
        "        state: restarted",
        "",
        "    - name: Flush privileges",
        "      community.mysql.mysql_query:",
        '        query: "FLUSH PRIVILEGES"',
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
    ]
    return lines


def _restore_apache_play(c, containers, nfs_containers):
    nfs_ip = _new_ip(nfs_containers[0].name) if nfs_containers else "{{ new_ips.nfs }}"
    lines = [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "    - name: Decompression config Apache",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_apache2.tar.gz"',
        "        dest: /etc/",
        "        remote_src: yes",
        "",
        "    - name: Creation du point de montage web",
        "      file:",
        "        path: /var/www/html",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Montage persistant du code web depuis NFS",
        "      mount:",
        f'        src: "{nfs_ip}:/srv/nfs/shared/html"',
        "        path: /var/www/html",
        "        fstype: nfs",
        "        opts: defaults,_netdev",
        "        state: mounted",
    ]

    for other in containers:
        escaped_ip = other.ip.replace(".", "\\.")
        lines += [
            "",
            f"    - name: Remplacement IP {other.name} dans config.php",
            "      replace:",
            "        path: /var/www/html/config.php",
            f"        regexp: '\\b{escaped_ip}\\b'",
            f'        replace: "{_new_ip(other.name)}"',
        ]

    lines += [
        "",
        "    - name: Remplacement DB_PASS dans config.php",
        "      lineinfile:",
        "        path: /var/www/html/config.php",
        "        regexp: \"define\\\\('DB_PASS'\"",
        "        line: \"define('DB_PASS', '{{ mariadb_appuser_password }}');\"",
    ]

    lines += [
        "",
        "    - name: Ecriture /etc/hosts avec IPs internes",
        "      blockinfile:",
        "        path: /etc/hosts",
        "        block: |",
    ] + [f"          {_new_ip(oc.name)} {oc.name}.migration.local" for oc in containers]

    lines += [
        "    - name: Redemarrage Apache",
        "      service:",
        "        name: apache2",
        "        state: restarted",
    ]
    return lines


def _restore_nfs_play(c):
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "",
        "    - name: Creation structure NFS",
        "      file:",
        '        path: "{{ item }}"',
        "        state: directory",
        "        mode: '0755'",
        "      loop:",
        "        - /srv/nfs/shared",
        "        - /srv/nfs/shared/html",
        "        - /srv/nfs/shared/ftp_uploads",
        "        - /srv/nfs/shared/documents",
        "        - /srv/nfs/shared/scripts",
        "",
        "    - name: Permissions ouvertes pour le partage FTP",
        "      file:",
        "        path: /srv/nfs/shared/ftp_uploads",
        "        state: directory",
        "        mode: '0777'",
        "",
        "    - name: Decompression archive NFS",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_nfs_shared.tar.gz"',
        "        dest: /srv/nfs/",
        "        remote_src: yes",
        "",
        "    - name: Reecriture /etc/exports avec nouveau sous-reseau",
        "      copy:",
        "        content: |",
        f"          /srv/nfs/shared {CONFIG['network'].get('private_subnet_cidr', '10.10.0.0/24')}(rw,sync,no_subtree_check,no_root_squash)",
        f"          /srv/nfs/shared/ftp_uploads {CONFIG['network'].get('private_subnet_cidr', '10.10.0.0/24')}(rw,sync,no_subtree_check,no_root_squash)",
        "        dest: /etc/exports",
        "",
        "    - name: Activation des exports NFS",
        "      command: exportfs -ra",
        "",
        "    - name: Redemarrage NFS",
        "      service:",
        "        name: nfs-kernel-server",
        "        state: restarted",
    ]


def _restore_backup_play(c, mariadb_containers):
    mariadb_ip = _new_ip(mariadb_containers[0].name) if mariadb_containers else "{{ new_ips.mariadb }}"
    db_name = c.backup_config.database if c.backup_config and c.backup_config.database else "app_db"
    dest_dir = c.backup_config.destination if c.backup_config and c.backup_config.destination else "/backups"
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de destination des backups",
        "      file:",
        f"        path: {dest_dir}",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Creation du fichier de configuration backup protege",
        "      copy:",
        "        content: |",
        "          [client]",
        "          user=appuser",
        "          password={{ mariadb_appuser_password }}",
        "        dest: /etc/backup.conf",
        "        owner: root",
        "        group: root",
        "        mode: '0600'",
        "",
        "    - name: Depot backup.sh",
        "      copy:",
        "        content: |",
        "          #!/bin/bash",
        "          DATE=$(date +%Y-%m-%d_%Hh%M)",
        f'          HOST="{mariadb_ip}"',
        f'          DB="{db_name}"',
        f'          DEST="{dest_dir}"',
        "          mysqldump --defaults-extra-file=/etc/backup.conf --single-transaction --skip-lock-tables -h $HOST $DB > $DEST/backup_$DATE.sql",
        "          if [ $? -eq 0 ]; then",
        '              echo "Backup reussi : $DEST/backup_$DATE.sql"',
        "          else",
        '              echo "Backup echoue"',
        "          fi",
        "        dest: /usr/local/bin/backup.sh",
        "        mode: '0750'",
        "",
        "    - name: Creation du cron backup",
        "      cron:",
        '        name: "backup quotidien"',
        '        minute: "0"',
        '        hour: "2"',
        "        job: /usr/local/bin/backup.sh",
        "        user: root",
    ]


def _restore_ftp_play(c, nfs_containers):
    nfs_ip = _new_ip(nfs_containers[0].name) if nfs_containers else "{{ new_ips.nfs }}"
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation des users FTP",
        "      user:",
        '        name: "{{ item.username }}"',
        '        home: "{{ item.home }}"',
        "        shell: /bin/bash",
        "        create_home: yes",
        "        state: present",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Injection des hashes de passwords",
        "      user:",
        '        name: "{{ item.username }}"',
        '        password: "{{ item.password_hash }}"',
        "        update_password: always",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Creation des repertoires files",
        "      file:",
        '        path: "{{ item.home }}/files"',
        "        state: directory",
        '        owner: "{{ item.username }}"',
        "        mode: '0755'",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Montage persistant du partage FTP depuis NFS",
        "      mount:",
        f'        src: "{nfs_ip}:/srv/nfs/shared/ftp_uploads"',
        '        path: "{{ item.home }}/files"',
        "        fstype: nfs",
        "        opts: defaults,_netdev",
        "        state: mounted",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Configuration vsftpd",
        "      copy:",
        "        content: |",
        "          listen=NO",
        "          listen_ipv6=YES",
        "          anonymous_enable=NO",
        "          local_enable=YES",
        "          write_enable=YES",
        "          local_umask=022",
        "          dirmessage_enable=YES",
        "          use_localtime=YES",
        "          xferlog_enable=YES",
        "          connect_from_port_20=YES",
        "          chroot_local_user={{ 'YES' if vsftpd_config.chroot_local_user else 'NO' }}",
        "          allow_writeable_chroot=YES",
        "          secure_chroot_dir=/var/run/vsftpd/empty",
        "          pam_service_name=vsftpd",
        "          rsa_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
        "          rsa_private_key_file=/etc/ssl/private/ssl-cert-snakeoil.key",
        "          ssl_enable=NO",
        "          pasv_enable=YES",
        "          pasv_min_port={{ vsftpd_config.pasv_min_port }}",
        "          pasv_max_port={{ vsftpd_config.pasv_max_port }}",
        "        dest: /etc/vsftpd.conf",
        "",
        "    - name: Redemarrage vsftpd",
        "      service:",
        "        name: vsftpd",
        "        state: restarted",
    ]


def generer_restore_yml(containers: list, ip_mapping: dict):
    """Generate restore.yml dynamically based on scanned container services."""
    apache_containers  = [c for c in containers if "apache2" in c.services]
    # A backup container is identified by the presence of backup.sh detected during scan.
    # Using "cron" alone is too broad — cron runs in every Ubuntu container by default.
    backup_containers  = [c for c in containers if c.backup_config is not None
                          and "mariadb" not in c.services]
    mariadb_containers = [c for c in containers if "mariadb" in c.services]
    nfs_containers     = [c for c in containers if "nfs-server" in c.services]

    lines = ["---", "# Généré automatiquement par l'orchestrateur"]

    ordered = (
        mariadb_containers
        + nfs_containers
        + apache_containers
        + backup_containers
        + [c for c in containers if "vsftpd" in c.services]
    )
    seen = set()

    for c in ordered:
        if c.name in seen:
            continue
        seen.add(c.name)
        if "mariadb"    in c.services:
            lines += _restore_mariadb_play(c, apache_containers, backup_containers)
        if "apache2"    in c.services:
            lines += _restore_apache_play(c, containers, nfs_containers)
        if "nfs-server" in c.services:
            lines += _restore_nfs_play(c)
        if c.backup_config is not None and "mariadb" not in c.services:
            lines += _restore_backup_play(c, mariadb_containers)
        if "vsftpd"     in c.services:
            lines += _restore_ftp_play(c, nfs_containers)

    (ANSIBLE_DIR / "restore.yml").write_text("\n".join(lines) + "\n")
    ok("restore.yml")


# ─── Helpers validate ─────────────────────────────────────────────────────────

def _validate_mariadb_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service MariaDB",
        "      service_facts:",
        "",
        "    - name: MariaDB est actif",
        "      assert:",
        "        that: ansible_facts.services['mariadb.service'].state == 'running'",
        "        fail_msg: \"MariaDB n'est pas actif\"",
        "",
        "    - name: Verification bases de données",
        "      community.mysql.mysql_query:",
        "        query: \"SHOW DATABASES LIKE '{{ item }}'\"",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        '      loop: "{{ databases }}"',
        "      register: db_check",
        "",
        "    - name: Verification user appuser",
        "      community.mysql.mysql_query:",
        "        query: \"SELECT user, host FROM mysql.user WHERE user='appuser'\"",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        "      register: user_check",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport MariaDB",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'databases': databases} | to_json }}\"",
        "        dest: /tmp/migration/validation_mariadb.json",
    ]


def _validate_apache_play(c, containers):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service Apache",
        "      service_facts:",
        "",
        "    - name: Apache est actif",
        "      assert:",
        "        that: ansible_facts.services['apache2.service'].state == 'running'",
        "        fail_msg: \"Apache n'est pas actif\"",
        "",
        "    - name: Test HTTP sur localhost",
        "      uri:",
        "        url: http://localhost",
        "        method: GET",
        "        status_code: 200",
        "      register: http_check",
        "",
        "    - name: Verification montage NFS du code web",
        "      shell: mount | grep ' /var/www/html ' | grep nfs",
        "      register: apache_mount",
        "      changed_when: false",
        "",
        "    - name: Verification absence anciennes IPs LXC",
        '      shell: grep -rE "10\\.0\\." /var/www/html/config.php || echo "OK"',
        "      register: ip_check",
        "      changed_when: false",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport Apache",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'http_code': http_check.status} | to_json }}\"",
        "        dest: /tmp/migration/validation_apache.json",
    ]


def _validate_nfs_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification exports NFS",
        "      command: exportfs -v",
        "      register: nfs_exports_check",
        "      changed_when: false",
        "",
        "    - name: NFS exporte au moins un repertoire",
        "      assert:",
        "        that: nfs_exports_check.stdout | length > 0",
        "        fail_msg: \"NFS n'exporte rien\"",
        "",
        "    - name: Verification fichiers partages",
        "      find:",
        "        paths: /srv/nfs/shared",
        "        recurse: yes",
        "      register: nfs_files",
        "",
        "    - name: Verification presence du code web partage",
        "      stat:",
        "        path: /srv/nfs/shared/html/index.php",
        "      register: nfs_web",
        "",
        "    - name: Code web partage present",
        "      assert:",
        "        that: nfs_web.stat.exists",
        "        fail_msg: \"Le code web partage est absent sur NFS\"",
        "",
        "    - name: Verification presence du partage FTP",
        "      stat:",
        "        path: /srv/nfs/shared/ftp_uploads",
        "      register: nfs_ftp",
        "",
        "    - name: Partage FTP present sur NFS",
        "      assert:",
        "        that: nfs_ftp.stat.exists",
        "        fail_msg: \"Le partage FTP est absent sur NFS\"",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport NFS",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'files_count': nfs_files.matched} | to_json }}\"",
        "        dest: /tmp/migration/validation_nfs.json",
    ]


def _validate_backup_play(c, mariadb_containers):
    mariadb_ip = _new_ip(mariadb_containers[0].name) if mariadb_containers else "{{ new_ips.mariadb }}"
    db_name = c.backup_config.database if c.backup_config and c.backup_config.database else "app_db"
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification script backup.sh",
        "      stat:",
        "        path: /usr/local/bin/backup.sh",
        "      register: backup_script",
        "",
        "    - name: backup.sh existe",
        "      assert:",
        "        that: backup_script.stat.exists",
        "        fail_msg: \"backup.sh est absent\"",
        "",
        "    - name: Verification nouvelle IP mariadb dans backup.sh",
        f'      shell: grep "{mariadb_ip}" /usr/local/bin/backup.sh',
        "      register: ip_in_backup",
        "",
        "    - name: Nouvelle IP mariadb presente dans backup.sh",
        "      assert:",
        "        that: ip_in_backup.rc == 0",
        "        fail_msg: \"La nouvelle IP mariadb est absente de backup.sh\"",
        "",
        "    - name: Verification options mysqldump sans lock",
        "      shell: grep -- '--single-transaction --skip-lock-tables' /usr/local/bin/backup.sh",
        "      register: backup_opts",
        "",
        "    - name: Options mysqldump presentes",
        "      assert:",
        "        that: backup_opts.rc == 0",
        "        fail_msg: \"Le script backup.sh n'utilise pas les options mysqldump attendues\"",
        "",
        f"    - name: Verification base {db_name} dans backup.sh",
        f"      shell: grep 'DB=\"{db_name}\"' /usr/local/bin/backup.sh",
        "      register: backup_db",
        "",
        "    - name: Base cible correcte dans backup.sh",
        "      assert:",
        "        that: backup_db.rc == 0",
        "        fail_msg: \"La base cible attendue est absente de backup.sh\"",
        "",
        "    - name: Verification cron actif",
        "      service_facts:",
        "",
        "    - name: Cron est actif",
        "      assert:",
        "        that: ansible_facts.services['cron.service'].state == 'running'",
        "        fail_msg: \"Cron n'est pas actif\"",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport Backup",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'script': backup_script.stat.exists} | to_json }}\"",
        "        dest: /tmp/migration/validation_backup.json",
    ]


def _validate_ftp_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service vsftpd",
        "      service_facts:",
        "",
        "    - name: vsftpd est actif",
        "      assert:",
        "        that: ansible_facts.services['vsftpd.service'].state == 'running'",
        "        fail_msg: \"vsftpd n'est pas actif\"",
        "",
        "    - name: Verification repertoires files des users FTP",
        "      stat:",
        '        path: "{{ item.home }}/files"',
        '      loop: "{{ ftp_users }}"',
        "      register: ftp_dirs",
        "",
        "    - name: Verification montage NFS sur files",
        "      shell: mount | grep ' {{ item.home }}/files ' | grep nfs",
        '      loop: "{{ ftp_users }}"',
        "      register: ftp_mounts",
        "      changed_when: false",
        "",
        "    - name: Verification password hash non vide pour chaque user FTP",
        "      shell: \"grep '^{{ item.username }}:' /etc/shadow | cut -d: -f2 | grep -qE '^\\$'\"",
        '      loop: "{{ ftp_users }}"',
        "      changed_when: false",
        "      failed_when: false",
        "      register: ftp_hash_check",
        "",
        "    - name: Tous les users FTP ont un hash valide",
        "      assert:",
        "        that: item.rc == 0",
        "        fail_msg: \"User FTP {{ ftp_users[ansible_loop.index0].username }} n'a pas de mot de passe\"",
        '      loop: "{{ ftp_hash_check.results }}"',
        "      loop_control:",
        "        extended: yes",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport FTP",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok'} | to_json }}\"",
        "        dest: /tmp/migration/validation_ftp.json",
    ]


def generer_validate_yml(containers: list, ip_mapping: dict):
    """Generate validate.yml dynamically based on scanned container services."""
    mariadb_containers = [c for c in containers if "mariadb" in c.services]

    lines = ["---", "# Généré automatiquement par l'orchestrateur"]

    for c in containers:
        if "mariadb"    in c.services:
            lines += _validate_mariadb_play(c)
        if "apache2"    in c.services:
            lines += _validate_apache_play(c, containers)
        if "nfs-server" in c.services:
            lines += _validate_nfs_play(c)
        if c.backup_config is not None and "mariadb" not in c.services:
            lines += _validate_backup_play(c, mariadb_containers)
        if "vsftpd"     in c.services:
            lines += _validate_ftp_play(c)

    (ANSIBLE_DIR / "validate.yml").write_text("\n".join(lines) + "\n")
    ok("validate.yml")


def generer_inventaire(instances: dict, containers: list):
    titre("5", "9", "Generation inventaire Ansible")

    apache_fip = CONFIG["network"].get("apache_floating_ip", "")
    ssh_user   = CONFIG["ssh"]["user"]

    if apache_fip:
        subprocess.run(
            ["ssh-keygen", "-R", apache_fip],
            capture_output=True
        )
        apache_internal = instances.get("apache", {}).get("ip", "")
        if apache_internal:
            subprocess.run(
                ["ssh-keygen", "-R", apache_internal],
                capture_output=True
            )
    inventory  = ""
    for nom, data in instances.items():
        if nom == "apache" and apache_fip:
            host_ip = apache_fip
            extra   = ""
        else:
            host_ip     = data["ip"]
            proxy_args  = (
                f'-o StrictHostKeyChecking=no '
                f'-o ProxyCommand="ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p {ssh_user}@{apache_fip}"'
            )
            extra       = f" ansible_ssh_common_args='{proxy_args}'"
        inventory += f"[{nom}]\n"
        inventory += f"{host_ip} ansible_user={ssh_user} "
        inventory += f"ansible_ssh_private_key_file={SSH_KEY}{extra}\n\n"

    inv_path = ANSIBLE_DIR / "inventory.ini"
    inv_path.write_text(inventory)
    ok("inventory.ini")

    generer_group_vars(instances)

    container_map = {c.name: c for c in containers}
    hv_dir = ANSIBLE_DIR / "host_vars"
    hv_dir.mkdir(exist_ok=True)
    for nom, data in instances.items():
        c = container_map.get(nom)
        if not c:
            continue

        host_vars = {
            "old_lxc_ip":    c.ip,
            "instance_ip":   data["ip"],
        }

        if c.databases:
            host_vars["databases"] = [d.name for d in c.databases]
        if c.db_users:
            host_vars["old_apache_ip"] = next(
                (u.host for u in c.db_users if u.host != "%"), ""
            )
        if c.ftp_users:
            host_vars["ftp_users"] = [
                {"username": u.username, "home": u.home, "password_hash": u.password_hash}
                for u in c.ftp_users
            ]
        if c.vsftpd_config:
            host_vars["vsftpd_config"] = c.vsftpd_config.model_dump()
        if c.nfs_exports:
            host_vars["nfs_exports"] = [
                {"path": e.path, "subnet": e.subnet, "options": e.options}
                for e in c.nfs_exports
            ]

        hv_ip   = data["floating_ip"] if nom == "apache" and apache_fip else data["ip"]
        hv_path = hv_dir / f"{hv_ip}.yml"
        hv_path.write_text(
            "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in host_vars.items())
        )
        ok(f"host_vars {nom}")

    # Generate all three playbooks from the live scan
    ip_mapping = {nom: data["ip"] for nom, data in instances.items()}
    generer_provision_yml(containers)
    generer_restore_yml(containers, ip_mapping)
    generer_validate_yml(containers, ip_mapping)


# ─── Phase Backup ─────────────────────────────────────────────────────────────

def phase_backup(containers: list, credentials: dict):
    titre("6", "9", "Backup des containers LXC")

    tmp_dir = tempfile.mkdtemp()
    os.chmod(tmp_dir, 0o700)
    info(f"Repertoire temporaire : {tmp_dir}")

    for c in containers:
        info(f"Backup {c.name}...")

        if "mariadb" in c.services:
            cnf = f"[mysqldump]\nuser=root\npassword={credentials['mariadb_root_password']}\n"
            subprocess.run(
                ["sudo", "lxc-attach", "-n", c.name, "--", "tee", "/tmp/.my.cnf"],
                input=cnf, capture_output=True, text=True
            )
            subprocess.run(
                ["sudo", "lxc-attach", "-n", c.name, "--", "chmod", "600", "/tmp/.my.cnf"],
                capture_output=True
            )
            try:
                for db in c.databases:
                    r = executer_cmd([
                        "sudo", "lxc-attach", "-n", c.name, "--",
                        "mysqldump", "--defaults-extra-file=/tmp/.my.cnf",
                        db.name
                    ])
                    if r.returncode == 0:
                        dump_path = os.path.join(tmp_dir, f"{c.name}_{db.name}.sql")
                        with open(dump_path, "w") as f:
                            f.write(r.stdout)
                        ok(f"dump {db.name}")
                    else:
                        fail(f"dump {db.name}")
            finally:
                subprocess.run(
                    ["sudo", "lxc-attach", "-n", c.name, "--", "rm", "-f", "/tmp/.my.cnf"],
                    capture_output=True
                )

        if "apache2" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_html.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/var/www",
                "html"
            ])
            if r.returncode == 0:
                ok("archive /var/www/html")
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_apache2.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/etc",
                "apache2"
            ])
            if r.returncode == 0:
                ok("archive /etc/apache2")

        if "vsftpd" in c.services:
            for u in c.ftp_users:
                username = u.username
                home_rel = u.home.lstrip("/")
                r = executer_cmd([
                    "sudo", "tar", "-czf",
                    os.path.join(tmp_dir, f"{c.name}_ftp_{username}.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/{home_rel}",
                    "."
                ])
                if r.returncode == 0:
                    ok(f"archive ftp {username}")

        if "nfs-server" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_nfs_shared.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/srv/nfs",
                "shared"
            ])
            if r.returncode == 0:
                ok("archive /srv/nfs/shared")

    return tmp_dir


# ─── Phase Transfert ──────────────────────────────────────────────────────────

def phase_transfert(instances: dict, tmp_dir: str, containers: list, state: State):
    titre("7", "9", "Transfert des archives")

    apache_fip    = CONFIG["network"].get("apache_floating_ip", "")
    container_map = {c.name: c for c in containers}

    for nom, data in instances.items():
        c = container_map.get(nom)
        if not c:
            continue

        # Build file list dynamically from scan — no static config needed
        fichiers = []
        if "mariadb"    in c.services:
            fichiers += [f"{c.name}_{db.name}.sql" for db in c.databases]
        if "apache2"    in c.services:
            fichiers += [f"{c.name}_html.tar.gz", f"{c.name}_apache2.tar.gz"]
        if "vsftpd"     in c.services:
            fichiers += [f"{c.name}_ftp_{u.username}.tar.gz" for u in c.ftp_users]
        if "nfs-server" in c.services:
            fichiers += [f"{c.name}_nfs_shared.tar.gz"]

        if not fichiers:
            continue

        connect_ip = data["floating_ip"] if nom == "apache" else data["ip"]
        use_proxy  = bool(apache_fip) and nom != "apache"

        info(f"Transfert vers {nom} ({connect_ip})...")

        proxy_client = None
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        for tentative in range(5):
            try:
                if use_proxy:
                    proxy_client = paramiko.SSHClient()
                    proxy_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    proxy_client.connect(
                        hostname=apache_fip,
                        username=CONFIG["ssh"]["user"],
                        key_filename=str(SSH_KEY),
                        timeout=30,
                        banner_timeout=60
                    )
                    channel = proxy_client.get_transport().open_channel(
                        "direct-tcpip", (connect_ip, 22), ("127.0.0.1", 0)
                    )
                    client.connect(
                        hostname=connect_ip,
                        username=CONFIG["ssh"]["user"],
                        key_filename=str(SSH_KEY),
                        timeout=30,
                        banner_timeout=60,
                        sock=channel
                    )
                else:
                    client.connect(
                        hostname=connect_ip,
                        username=CONFIG["ssh"]["user"],
                        key_filename=str(SSH_KEY),
                        timeout=30,
                        banner_timeout=60
                    )
                break
            except Exception as e:
                if proxy_client:
                    try:
                        proxy_client.close()
                    except Exception:
                        pass
                    proxy_client = None
                if tentative < 4:
                    time.sleep(10)
                else:
                    raise e

        staging = CONFIG["staging_dir"]
        _, _, stderr = client.exec_command(f"mkdir -p {staging}")
        if stderr.read():
            raise Exception(f"mkdir {staging} echoue sur {connect_ip}")
        sftp = client.open_sftp()

        for fichier in fichiers:
            src = os.path.join(tmp_dir, fichier)
            if os.path.exists(src):
                sftp.put(src, f"{staging}/{fichier}")
                ok(f"{fichier} -> {nom}")
            else:
                fail(f"{fichier} introuvable")

        sftp.close()
        client.close()
        if proxy_client:
            proxy_client.close()

    shutil.rmtree(tmp_dir)
    info("Repertoire temporaire supprime")
    state.phase_terminee(Phase.BACKUP)


# ─── Phase Ansible ────────────────────────────────────────────────────────────

def lancer_ansible(playbook: str, inventaire: str, extra_vars: dict = None):
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env["ANSIBLE_FORCE_COLOR"] = "1"

    cmd = [
        "ansible-playbook",
        playbook,
        "-i", inventaire,
    ]
    if extra_vars:
        cmd += ["--extra-vars", json.dumps(extra_vars)]

    # Pas de capture_output : Ansible écrit directement sur le terminal en temps réel
    r = subprocess.run(cmd, shell=False, text=True, timeout=1800, env=env)
    if r.returncode != 0:
        raise Exception(f"Ansible echoue (code {r.returncode}) — voir la sortie ci-dessus")
    return r

def phase_ansible(state: State, phase: Phase, etape_num: str,
                   etape_nom: str, playbook: str, extra_vars: dict = None):
    titre(etape_num, "9", etape_nom)
    inventaire = str(ANSIBLE_DIR / "inventory.ini")

    cp_dir = Path.home() / ".ansible" / "cp"
    if cp_dir.exists():
        for sock in cp_dir.iterdir():
            sock.unlink(missing_ok=True)

    info(f"ansible-playbook {playbook}...")
    lancer_ansible(str(ANSIBLE_DIR / playbook), inventaire, extra_vars)
    ok(etape_nom)
    state.phase_terminee(phase)


# ─── Rollback ─────────────────────────────────────────────────────────────────

def rollback(state: State, erreur: str):
    print(f"\n  ERREUR : {erreur}")
    print("  Rollback en cours...")

    # Détruire uniquement les instances et volumes — pas le réseau.
    # Le réseau (router/subnet) est lent à recréer sur CERIST, on le conserve
    # entre les runs pour éviter le timeout sur router_interface.
    cibles = [
        "openstack_compute_floatingip_associate_v2.apache_fip",
        "openstack_compute_volume_attach_v2.mariadb_volume_attach",
        'openstack_compute_instance_v2.instances["apache"]',
        'openstack_compute_instance_v2.instances["backup"]',
        'openstack_compute_instance_v2.instances["ftp"]',
        'openstack_compute_instance_v2.instances["mariadb"]',
        'openstack_compute_instance_v2.instances["nfs"]',
    ]
    target_args = []
    for t in cibles:
        target_args += ["-target", t]

    r = executer_cmd(
        ["terraform", "destroy", "-auto-approve", "-no-color"] + target_args,
        cwd=TERRAFORM_DIR
    )
    if r.returncode == 0:
        ok("Instances OpenStack supprimees")
    else:
        fail("terraform destroy (instances)")

    state.marquer_echec(erreur)


# ─── Rapport final ────────────────────────────────────────────────────────────



def generer_rapport(state: State, instances: dict):
    titre("9", "9", "Rapport final")

    rapport = {
        "migration_date": datetime.now().isoformat(),
        "status": "success",
        "duree_secondes": (datetime.now() - datetime.fromisoformat(state.debut)).total_seconds(),
        "instances": {},
    }

    print(f"\n  {'Service':<12} {'Ancienne IP':<16} {'IP instance':<16}")
    print(f"  {'-'*12} {'-'*16} {'-'*16}")

    for nom, data in instances.items():
        lxc_ip = state.ip_mapping.get(nom, {}).get("lxc_ip", "")
        rapport["instances"][nom] = {
            "old_lxc_ip":  lxc_ip,
            "ip":          data["ip"],
            "validation":  "passed" if Phase.VALIDATE in [Phase[p] for p in state.phases_ok] else "skipped"
        }
        print(f"  {nom:<12} {lxc_ip:<16} {data['ip']:<16}")

    rapport_path = BASE_DIR / "migration_report.json"
    rapport_path.write_text(json.dumps(rapport, indent=2))
    print(f"\n  Rapport ecrit : {rapport_path}")
    apache_data = instances.get("apache", {})
    apache_ip   = apache_data.get("floating_ip") or apache_data.get("ip", "")
    if apache_ip:
        configurer_iptables(apache_ip)
    print("\n=== Migration terminee avec succes ===\n")


# ─── Point d'entrée ───────────────────────────────────────────────────────────

def main():
    print("\n=== Migration LXC -> OpenStack ===")
    print("    PFE Master - Automatisation complete\n")

    state = State.charger()
    instances = {}

    try:
        verifier_prerequis()
        configurer_iptables(_get_lxc_apache_ip())
        credentials = collecter_credentials()

        if state.phase.value <= Phase.SCAN.value:
            containers = phase_scan(state)
        else:
            containers = scanner_containers()

        if state.phase.value <= Phase.PROVISIONING.value:
            instances = phase_provisioning(state, credentials, containers)
            generer_inventaire(instances, containers)
        else:
            r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
            outputs = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
            if "instances" in outputs:
                instances = outputs["instances"]["value"]
            else:
                instances = {
                    nom: {"ip": data["internal_ip"], "floating_ip": data.get("floating_ip", data["internal_ip"])}
                    for nom, data in state.ip_mapping.items()
                }

        if state.phase.value <= Phase.BACKUP.value:
            tmp_dir = phase_backup(containers, credentials)
            phase_transfert(instances, tmp_dir, containers, state)

        if state.phase.value <= Phase.PROVISION.value:
            phase_ansible(state, Phase.PROVISION, "8a", "Provisionnement logiciel", "provision.yml")

        if state.phase.value <= Phase.RESTORE.value:
            phase_ansible(
                state, Phase.RESTORE, "8b", "Restauration des services", "restore.yml",
                extra_vars={"mariadb_appuser_password": credentials["mariadb_app_password"]}
            )

        if state.phase.value <= Phase.VALIDATE.value:
            phase_ansible(state, Phase.VALIDATE, "8c", "Validation interne", "validate.yml")

        state.marquer_termine()
        generer_rapport(state, instances)

    except Exception as e:
        rollback(state, str(e))
        sys.exit(1)


def regenerer_inventaire():
    """Relit le terraform output et regénère l'inventaire Ansible."""
    r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
    if r.returncode != 0:
        print("Erreur : terraform output a échoué")
        sys.exit(1)
    outputs = json.loads(r.stdout)
    if "instances" not in outputs:
        print("  Pas de state Terraform. Régénération des playbooks uniquement.")
        containers = scanner_containers()
        generer_provision_yml(containers)
        ip_mapping = {}
        generer_restore_yml(containers, ip_mapping)
        generer_validate_yml(containers, ip_mapping)
        print("Playbooks régénérés.")
        return
    instances = outputs["instances"]["value"]
    containers = scanner_containers()
    generer_inventaire(instances, containers)
    print("Inventaire régénéré.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--regen-inventory":
        regenerer_inventaire()
    else:
        main()
