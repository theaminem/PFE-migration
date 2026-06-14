# ─── Provider OpenStack ───────────────────────────────────────────────────────

terraform {
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.54"
    }
  }
}

provider "openstack" {
  auth_url          = var.os_auth_url
  user_name         = var.os_username
  password          = var.os_password
  tenant_name       = var.os_project_name
  tenant_id         = var.os_project_id
  user_domain_name  = var.os_user_domain_name
  project_domain_id = var.os_project_domain_id
  region            = var.os_region
}

# ─── Réseau privé interne ─────────────────────────────────────────────────────

resource "openstack_networking_network_v2" "migration_net" {
  name           = "migration-net"
  admin_state_up = true
}

resource "openstack_networking_subnet_v2" "migration_subnet" {
  name            = "migration-subnet"
  network_id      = openstack_networking_network_v2.migration_net.id
  cidr            = var.private_subnet_cidr
  ip_version      = 4
  dns_nameservers = ["8.8.8.8", "8.8.4.4"]
}

# ─── Router vers le réseau public ─────────────────────────────────────────────

resource "openstack_networking_router_v2" "migration_router" {
  name                = "migration-router"
  admin_state_up      = true
  external_network_id = var.external_network_id
}

resource "openstack_networking_router_interface_v2" "migration_iface" {
  router_id = openstack_networking_router_v2.migration_router.id
  subnet_id = openstack_networking_subnet_v2.migration_subnet.id

  timeouts {
    create = "20m"
    delete = "20m"
  }
}

# ─── Clé SSH ──────────────────────────────────────────────────────────────────

resource "openstack_compute_keypair_v2" "migration_key" {
  name       = "migration-key"
  public_key = var.ssh_public_key
}

resource "openstack_blockstorage_volume_v3" "mariadb_volume" {
  for_each    = local.mariadb_instances
  name        = "mariadb-data-${each.key}"
  size        = 10
  description = "Volume de données MariaDB pour ${each.key}"
}

# ─── Security Groups ──────────────────────────────────────────────────────────

resource "openstack_networking_secgroup_v2" "sg_mariadb" {
  name        = "sg-mariadb"
  description = "MariaDB : SSH + 3306"
}

resource "openstack_networking_secgroup_rule_v2" "sg_mariadb_ssh" {
  security_group_id = openstack_networking_secgroup_v2.sg_mariadb.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_mariadb_3306" {
  security_group_id = openstack_networking_secgroup_v2.sg_mariadb.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 3306
  port_range_max    = 3306
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_v2" "sg_apache" {
  name        = "sg-apache"
  description = "Apache : SSH + 80 + 443"
}

resource "openstack_networking_secgroup_rule_v2" "sg_apache_ssh" {
  security_group_id = openstack_networking_secgroup_v2.sg_apache.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_apache_80" {
  security_group_id = openstack_networking_secgroup_v2.sg_apache.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 80
  port_range_max    = 80
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_apache_443" {
  security_group_id = openstack_networking_secgroup_v2.sg_apache.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_v2" "sg_backup" {
  name        = "sg-backup"
  description = "Backup : SSH uniquement"
}

resource "openstack_networking_secgroup_rule_v2" "sg_backup_ssh" {
  security_group_id = openstack_networking_secgroup_v2.sg_backup.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_v2" "sg_ftp" {
  name        = "sg-ftp"
  description = "FTP : SSH + 21 + ports passifs"
}

resource "openstack_networking_secgroup_rule_v2" "sg_ftp_ssh" {
  security_group_id = openstack_networking_secgroup_v2.sg_ftp.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_ftp_21" {
  security_group_id = openstack_networking_secgroup_v2.sg_ftp.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 21
  port_range_max    = 21
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_ftp_passif" {
  security_group_id = openstack_networking_secgroup_v2.sg_ftp.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 40000
  port_range_max    = 40100
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_v2" "sg_nfs" {
  name        = "sg-nfs"
  description = "NFS : SSH + 2049 + 111"
}

resource "openstack_networking_secgroup_rule_v2" "sg_nfs_ssh" {
  security_group_id = openstack_networking_secgroup_v2.sg_nfs.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_nfs_2049" {
  security_group_id = openstack_networking_secgroup_v2.sg_nfs.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 2049
  port_range_max    = 2049
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "openstack_networking_secgroup_rule_v2" "sg_nfs_111" {
  security_group_id = openstack_networking_secgroup_v2.sg_nfs.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 111
  port_range_max    = 111
  remote_ip_prefix  = "0.0.0.0/0"
}

# ─── Mapping container → security group ──────────────────────────────────────

locals {
  secgroup_map = {
    mariadb = openstack_networking_secgroup_v2.sg_mariadb.id
    apache  = openstack_networking_secgroup_v2.sg_apache.id
    backup  = openstack_networking_secgroup_v2.sg_backup.id
    ftp     = openstack_networking_secgroup_v2.sg_ftp.id
    nfs     = openstack_networking_secgroup_v2.sg_nfs.id
  }
  mariadb_instances   = { for k, v in var.instances : k => v if v.service_type == "mariadb" }
  apache_instance_key = var.gateway_instance
}

# ─── Instances Nova ───────────────────────────────────────────────────────────

resource "openstack_compute_instance_v2" "instances" {
  for_each        = var.instances
  name            = "instance-${each.key}"
  image_name      = var.image_name
  flavor_name     = each.value.flavor
  key_pair        = openstack_compute_keypair_v2.migration_key.name
  security_groups = [local.secgroup_map[each.value.service_type]]

  network {
    uuid = openstack_networking_network_v2.migration_net.id
  }

  depends_on = [openstack_networking_router_interface_v2.migration_iface]

  timeouts {
    create = "10m"
    delete = "10m"
  }
}

resource "openstack_compute_volume_attach_v2" "mariadb_volume_attach" {
  for_each    = local.mariadb_instances
  instance_id = openstack_compute_instance_v2.instances[each.key].id
  volume_id   = openstack_blockstorage_volume_v3.mariadb_volume[each.key].id

  timeouts {
    create = "10m"
    delete = "20m"
  }
}

# ─── Floating IP pour Apache (porte d'entrée SSH) ─────────────────────────────

resource "openstack_compute_floatingip_associate_v2" "apache_fip" {
  floating_ip = var.apache_floating_ip
  instance_id = openstack_compute_instance_v2.instances[local.apache_instance_key].id
}
