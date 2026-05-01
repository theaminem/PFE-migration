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
  auth_url    = var.os_auth_url
  user_name   = var.os_username
  password    = var.os_password
  tenant_name = var.os_project_name
  region      = var.os_region
}

# ─── Réseau interne ───────────────────────────────────────────────────────────

resource "openstack_networking_network_v2" "migration_net" {
  name           = "migration-net"
  admin_state_up = true
}

resource "openstack_networking_subnet_v2" "migration_subnet" {
  name            = "migration-subnet"
  network_id      = openstack_networking_network_v2.migration_net.id
  cidr            = var.migration_network_cidr
  gateway_ip      = var.migration_gateway
  ip_version      = 4
  dns_nameservers = ["8.8.8.8"]

  allocation_pools {
    start = "10.10.10.110"
    end   = "10.10.10.200"
  }
}

# ─── Router ───────────────────────────────────────────────────────────────────

resource "openstack_networking_router_v2" "migration_router" {
  name                = "migration-router"
  admin_state_up      = true
  external_network_id = data.openstack_networking_network_v2.provider.id
}

resource "openstack_networking_router_interface_v2" "migration_router_iface" {
  router_id = openstack_networking_router_v2.migration_router.id
  subnet_id = openstack_networking_subnet_v2.migration_subnet.id
}

# ─── Réseau provider (référence) ─────────────────────────────────────────────

data "openstack_networking_network_v2" "provider" {
  name = var.provider_network
}

# ─── Clé SSH ──────────────────────────────────────────────────────────────────

resource "openstack_compute_keypair_v2" "migration_key" {
  name       = "migration-key"
  public_key = var.ssh_public_key
}

# ─── Security Groups ──────────────────────────────────────────────────────────

resource "openstack_networking_secgroup_v2" "sg_mariadb" {
  name        = "sg-mariadb"
  description = "MariaDB : SSH + 3306 depuis réseau interne"
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
  remote_ip_prefix  = var.migration_network_cidr
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
  remote_ip_prefix  = var.migration_network_cidr
}

resource "openstack_networking_secgroup_rule_v2" "sg_nfs_111" {
  security_group_id = openstack_networking_secgroup_v2.sg_nfs.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 111
  port_range_max    = 111
  remote_ip_prefix  = var.migration_network_cidr
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
}

# ─── Ports réseau avec IPs fixes ─────────────────────────────────────────────

resource "openstack_networking_port_v2" "ports" {
  for_each           = var.instances
  name               = "port-${each.key}"
  network_id         = openstack_networking_network_v2.migration_net.id
  admin_state_up     = true
  security_group_ids = [local.secgroup_map[each.key]]

  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.migration_subnet.id
    ip_address = each.value.internal_ip
  }
}

# ─── Instances Nova ───────────────────────────────────────────────────────────

resource "openstack_compute_instance_v2" "instances" {
  for_each        = var.instances
  name            = "instance-${each.key}"
  image_name      = var.image_name
  flavor_name     = each.value.flavor
  key_pair        = openstack_compute_keypair_v2.migration_key.name

  network {
    port = openstack_networking_port_v2.ports[each.key].id
  }
}

# ─── Floating IPs ─────────────────────────────────────────────────────────────

resource "openstack_networking_floatingip_v2" "floating_ips" {
  for_each = var.instances
  pool     = var.provider_network
}

resource "openstack_networking_floatingip_associate_v2" "fip_assoc" {
  for_each    = var.instances
  floating_ip = openstack_networking_floatingip_v2.floating_ips[each.key].address
  port_id     = openstack_networking_port_v2.ports[each.key].id
}
