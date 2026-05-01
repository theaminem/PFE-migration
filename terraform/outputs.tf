# ─── Outputs par instance ─────────────────────────────────────────────────────

output "instances" {
  description = "IPs internes et floating IPs de chaque instance"
  value = {
    for nom, instance in openstack_compute_instance_v2.instances : nom => {
      internal_ip = var.instances[nom].internal_ip
      floating_ip = openstack_networking_floatingip_v2.floating_ips[nom].address
      instance_id = instance.id
    }
  }
}
