# ─── Outputs par instance ─────────────────────────────────────────────────────

output "instances" {
  description = "IP privée et floating IP de chaque instance"
  value = {
    for k, v in openstack_compute_instance_v2.instances :
    k => {
      ip          = v.access_ip_v4
      floating_ip = k == "apache" ? openstack_compute_floatingip_associate_v2.apache_fip.floating_ip : v.access_ip_v4
    }
  }
}

output "mariadb_volume_device" {
  value = openstack_compute_volume_attach_v2.mariadb_volume_attach.device
}
