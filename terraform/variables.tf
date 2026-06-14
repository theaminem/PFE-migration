# ─── Image et clé SSH ─────────────────────────────────────────────────────────

variable "image_name" {
  description = "Image de base pour les instances"
  type        = string
  default     = "ubuntu-noble-24.04-amd64"
}

variable "ssh_public_key" {
  description = "Clé publique SSH pour les instances"
  type        = string
  default     = ""
}

# ─── Instances ────────────────────────────────────────────────────────────────

variable "instances" {
  description = "Configuration des instances à créer"
  type = map(object({
    flavor       = string
    service_type = string
  }))
  default = {
    mariadb = { flavor = "m1.medium", service_type = "mariadb" }
    apache  = { flavor = "m1.medium", service_type = "apache"  }
    backup  = { flavor = "m1.medium", service_type = "backup"  }
    ftp     = { flavor = "m1.medium", service_type = "ftp"     }
    nfs     = { flavor = "m1.medium", service_type = "nfs"     }
  }
}

# ─── Réseau ───────────────────────────────────────────────────────────────────

variable "external_network_id" {
  description = "ID du réseau public externe (PublicNetwork)"
  type        = string
}

variable "apache_floating_ip" {
  description = "Floating IP pré-allouée pour l'instance Apache (porte d'entrée SSH)"
  type        = string
}

variable "gateway_instance" {
  description = "Nom de l'instance qui reçoit la FloatingIP (gateway SSH)"
  type        = string
}

variable "private_subnet_cidr" {
  description = "CIDR du sous-réseau privé interne des instances"
  type        = string
  default     = "10.10.0.0/24"
}

# ─── Credentials OpenStack ────────────────────────────────────────────────────

variable "os_auth_url" {
  description = "URL d'authentification Keystone"
  type        = string
}

variable "os_username" {
  description = "Nom d'utilisateur OpenStack"
  type        = string
}

variable "os_password" {
  description = "Mot de passe OpenStack"
  type        = string
  sensitive   = true
}

variable "os_project_name" {
  description = "Nom du projet/tenant OpenStack"
  type        = string
}

variable "os_project_id" {
  description = "ID du projet/tenant OpenStack"
  type        = string
  default     = ""
}

variable "os_user_domain_name" {
  description = "Nom de domaine de l'utilisateur"
  type        = string
  default     = "Default"
}

variable "os_project_domain_id" {
  description = "ID de domaine du projet"
  type        = string
  default     = "default"
}

variable "os_region" {
  description = "Région OpenStack"
  type        = string
  default     = "RegionOne"
}
