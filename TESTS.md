# Tests post-migration

## 0. Préparer la session

```bash
# Sourcer les IPs depuis le rapport (à refaire à chaque nouveau run)
eval $(python3 -c "
import json
d = json.load(open('migration_report.json'))['instances']
fip = d['apache'].get('floating_ip', d['apache']['ip'])
print(f'FIP={fip}')
print(f'MARIADB={d[\"mariadb\"][\"ip\"]}')
print(f'NFS={d[\"nfs\"][\"ip\"]}')
print(f'FTP={d[\"ftp\"][\"ip\"]}')
print(f'BACKUP={d[\"backup\"][\"ip\"]}')
")
KEY=~/.ssh/migration_key
PROXY="ssh -i $KEY -o StrictHostKeyChecking=no -W %h:22 ubuntu@$FIP"

echo "FIP=$FIP  MARIADB=$MARIADB  NFS=$NFS  FTP=$FTP  BACKUP=$BACKUP"
```

---

## 1. Apache

```bash
# Site web accessible
curl -I http://$FIP

# SSH dans l'instance
ssh -i $KEY ubuntu@$FIP

    systemctl is-active apache2
    mount | grep '/var/www/html'                          # NFS monté sur le code web
    grep '10\.0\.3\.' /var/www/html/config.php || echo OK # aucune ancienne IP LXC
    curl -o /dev/null -sw "%{http_code}\n" http://localhost
```

---

## 2. MariaDB

```bash
ssh -i $KEY -o ProxyCommand="$PROXY" ubuntu@$MARIADB

    systemctl is-active mariadb
    lsblk                                                 # sdb doit apparaître monté sur /var/lib/mysql
    mount | grep '/var/lib/mysql'                         # volume Cinder monté
    df -h /var/lib/mysql                                  # ~10 Go
    sudo mysql -e "SHOW DATABASES;"                       # app_db et sysmonitor présentes
    sudo mysql -e "SELECT user,host FROM mysql.user WHERE user='appuser';"
    sudo grep 'bind-address' /etc/mysql/mariadb.conf.d/50-server.cnf
```

---

## 3. NFS

```bash
ssh -i $KEY -o ProxyCommand="$PROXY" ubuntu@$NFS

    systemctl is-active nfs-kernel-server
    sudo exportfs -v                                      # exports sur 10.10.0.0/24
    ls /srv/nfs/shared/html/                              # fichiers web présents
    ls /srv/nfs/shared/ftp_uploads/                       # répertoire FTP présent
```

---

## 4. FTP

```bash
ssh -i $KEY -o ProxyCommand="$PROXY" ubuntu@$FTP

    systemctl is-active vsftpd
    id ftpuser; id ftpuser1; id ftpuser2                  # users créés
    sudo grep -E '^ftpuser' /etc/shadow | cut -d: -f1,2  # hashes présents ($y$...)
    mount | grep nfs                                      # un montage NFS par user
    grep -E 'pasv_min|chroot_local' /etc/vsftpd.conf
```

---

## 5. Backup

```bash
ssh -i $KEY -o ProxyCommand="$PROXY" ubuntu@$BACKUP

    ls -la /usr/local/bin/backup.sh
    grep 'HOST=' /usr/local/bin/backup.sh                 # doit afficher $MARIADB
    grep 'DB=' /usr/local/bin/backup.sh                   # app_db
    systemctl is-active cron
    sudo crontab -l                                        # job cron présent
    sudo /usr/local/bin/backup.sh                          # test manuel
    ls -lh /backups/                                       # fichier créé
```

---

## 6. Connectivité inter-services

```bash
ssh -i $KEY ubuntu@$FIP

    for ip in $MARIADB $NFS $FTP $BACKUP; do
        ping -c1 -W2 $ip &>/dev/null && echo "$ip OK" || echo "$ip ECHEC"
    done
    nc -zv $MARIADB 3306
    showmount -e $NFS
```
