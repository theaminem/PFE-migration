#!/bin/bash
# Nettoyage post-migration : supprime les ressources OpenStack et les artefacts locaux

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/terraform"
ANSIBLE_DIR="$SCRIPT_DIR/ansible"
STATE_FILE="$SCRIPT_DIR/migration_state.json"

# ─── Détection dynamique des conteneurs migrés ────────────────────────────────
# Source 1 : migration_state.json (contient service_type → volumes MariaDB)
# Source 2 : terraform.tfstate  (fallback si state.json absent)

NOMS_CONTENEURS=()
NOMS_MARIADB=()

if [[ -f "$STATE_FILE" ]]; then
    mapfile -t NOMS_CONTENEURS < <(python3 -c "
import json, sys
data = json.load(open('$STATE_FILE'))
for name in data.get('ip_mapping', {}):
    print(name)
" 2>/dev/null)
    mapfile -t NOMS_MARIADB < <(python3 -c "
import json, sys
data = json.load(open('$STATE_FILE'))
for name, info in data.get('ip_mapping', {}).items():
    if info.get('service_type') == 'mariadb':
        print(name)
" 2>/dev/null)
fi

if [[ ${#NOMS_CONTENEURS[@]} -eq 0 && -f "$TERRAFORM_DIR/terraform.tfstate" ]]; then
    mapfile -t NOMS_CONTENEURS < <(python3 -c "
import json, sys
data = json.load(open('$TERRAFORM_DIR/terraform.tfstate'))
for res in data.get('resources', []):
    if res.get('type') == 'openstack_compute_instance_v2' and res.get('name') == 'instances':
        for inst in res.get('instances', []):
            key = inst.get('index_key', '')
            if key: print(key)
" 2>/dev/null)
    mapfile -t NOMS_MARIADB < <(python3 -c "
import json, sys
data = json.load(open('$TERRAFORM_DIR/terraform.tfstate'))
for res in data.get('resources', []):
    if res.get('type') == 'openstack_blockstorage_volume_v3' and res.get('name') == 'mariadb_volume':
        for inst in res.get('instances', []):
            key = inst.get('index_key', '')
            if key: print(key)
" 2>/dev/null)
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

confirmer() {
    local question="$1"
    local reponse
    echo ""
    read -r -p "  $question [o/N] : " reponse
    [[ "$reponse" =~ ^[oO]$ ]]
}

# Supprime une ressource OpenStack par nom.
# $1 peut contenir des espaces (ex: "security group") — word-splittage via tableau.
# Distingue "not found" (warn silencieux) de vraie erreur (message explicite).
os_delete() {
    local type="$1"
    local name="$2"
    local -a type_args
    read -ra type_args <<< "$type"
    local output
    if output=$(openstack "${type_args[@]}" delete "$name" 2>&1); then
        ok "Supprimé : $type '$name'"
    else
        if echo "$output" | grep -qiE "No [A-Za-z]+ with a name or ID|Could not find|404"; then
            warn "Déjà absent : $type '$name'"
        else
            fail "Erreur : $type '$name'"
            echo "        → $output" >&2
        fi
    fi
}

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        Nettoyage post-migration LXC -> OpenStack     ║"
echo "╚══════════════════════════════════════════════════════╝"

# ─── Étape 1 : Nettoyage cloud ────────────────────────────────────────────────

echo ""
echo "─── Étape 1 : Suppression des ressources OpenStack ───"
echo ""
echo "  Ressources ciblées :"
if [[ ${#NOMS_CONTENEURS[@]} -gt 0 ]]; then
    for n in "${NOMS_CONTENEURS[@]}"; do echo "    • Instance : instance-$n"; done
    for n in "${NOMS_MARIADB[@]}";    do echo "    • Volume   : mariadb-data-$n"; done
else
    echo "    • (aucun conteneur détecté — migration_state.json et terraform.tfstate absents)"
fi
echo "    • Keypair      : migration-key"
echo "    • Sec. groups  : sg-mariadb, sg-apache, sg-backup, sg-ftp, sg-nfs"
echo "    • Réseau       : migration-router, migration-subnet, migration-net"

SKIP_CLOUD=0

# Vérification des variables d'environnement
if [[ -z "${OS_AUTH_URL:-}" || -z "${OS_USERNAME:-}" || -z "${OS_PASSWORD:-}" ]]; then
    fail "Variables d'environnement OpenStack manquantes."
    echo ""
    echo "  Lance d'abord :"
    echo "    source ~/stdmigration-openrc.sh"
    echo "  puis relance ce script."
    SKIP_CLOUD=1
fi

if [[ $SKIP_CLOUD -eq 0 ]] && ! command -v openstack &>/dev/null; then
    fail "CLI 'openstack' introuvable dans le PATH."
    SKIP_CLOUD=1
fi

# Test de connexion réel — évite le faux silence si le mot de passe est mauvais
if [[ $SKIP_CLOUD -eq 0 ]]; then
    echo ""
    echo -n "  Test de connexion OpenStack... "
    if ! openstack token issue -f value -c id &>/dev/null; then
        echo ""
        fail "Connexion refusée par Keystone."
        echo "  Vérifie ton mot de passe OpenStack et relance :"
        echo "    source ~/stdmigration-openrc.sh && bash cleanup.sh"
        SKIP_CLOUD=1
    else
        echo -e "${GREEN}OK${NC}"
    fi
fi

if [[ $SKIP_CLOUD -eq 0 ]]; then
    if confirmer "Supprimer toutes les ressources OpenStack de la migration ?"; then

        echo ""
        echo "  Suppression des instances..."
        if [[ ${#NOMS_CONTENEURS[@]} -gt 0 ]]; then
            for name in "${NOMS_CONTENEURS[@]}"; do
                os_delete "server" "instance-$name"
            done
        else
            warn "Aucun conteneur détecté — instances non supprimées automatiquement."
            warn "Lance 'terraform destroy' manuellement si des instances subsistent."
        fi

        echo ""
        echo "  Attente de la suppression complète des instances (max 2 min)..."
        for name in "${NOMS_CONTENEURS[@]}"; do
            for i in $(seq 1 24); do
                if ! openstack server show "instance-$name" &>/dev/null; then
                    break
                fi
                sleep 5
            done
        done
        ok "Instances supprimées (ou déjà absentes)"

        echo ""
        echo "  Suppression des volumes Cinder..."
        if [[ ${#NOMS_MARIADB[@]} -gt 0 ]]; then
            for name in "${NOMS_MARIADB[@]}"; do
                os_delete "volume" "mariadb-data-$name"
            done
        else
            ok "Aucun volume MariaDB détecté"
        fi

        echo ""
        echo "  Suppression de la keypair..."
        os_delete "keypair" "migration-key"

        echo ""
        echo "  Suppression des security groups..."
        for name in sg-mariadb sg-apache sg-backup sg-ftp sg-nfs; do
            os_delete "security group" "$name"
        done

        # Suppression réseau : ordre strict imposé par Neutron
        echo ""
        echo "  Suppression du réseau (router → subnet → network)..."

        if openstack router remove subnet migration-router migration-subnet 2>/dev/null; then
            ok "Interface router détachée"
        else
            warn "Interface router déjà absente ou inexistante"
        fi

        if openstack router delete migration-router 2>/dev/null; then
            ok "Router supprimé"
        else
            warn "Router déjà absent ou inexistant"
        fi

        if openstack subnet delete migration-subnet 2>/dev/null; then
            ok "Subnet supprimé"
        else
            warn "Subnet déjà absent ou inexistant"
        fi

        if openstack network delete migration-net 2>/dev/null; then
            ok "Réseau supprimé"
        else
            warn "Réseau déjà absent ou inexistant"
        fi

        # Terraform destroy pour purger le tfstate
        if [[ -f "$TERRAFORM_DIR/terraform.tfstate" ]] && command -v terraform &>/dev/null; then
            echo ""
            echo "  Synchronisation du tfstate Terraform..."
            terraform -chdir="$TERRAFORM_DIR" init -no-color -input=false 1>/dev/null 2>&1 || true
            terraform -chdir="$TERRAFORM_DIR" destroy -auto-approve -no-color 1>/dev/null 2>&1 || true
            ok "tfstate purgé"
        fi

    else
        warn "Nettoyage cloud ignoré."
    fi
fi

# ─── Étape 2 : Nettoyage des artefacts locaux ─────────────────────────────────

echo ""
echo "─── Étape 2 : Suppression des artefacts locaux ───"
echo ""
echo "  Fichiers qui seront supprimés :"
echo "    • migration_state.json"
echo "    • migration_report.json"
echo "    • terraform/terraform.tfstate"
echo "    • terraform/terraform.tfstate.backup"
echo "    • ansible/inventory.ini"
echo "    • ansible/group_vars/all.yml"
echo "    • ansible/host_vars/*.yml  (IPs cloud, les 10.0.*.yml LXC sont conservés)"
echo "    • ansible/provision.yml"
echo "    • ansible/restore.yml"
echo "    • ansible/validate.yml"

if confirmer "Supprimer tous ces artefacts ?"; then

    for f in \
        "$SCRIPT_DIR/migration_state.json" \
        "$SCRIPT_DIR/migration_report.json"
    do
        if [[ -f "$f" ]]; then
            rm -f "$f"
            ok "Supprimé : $(basename "$f")"
        else
            warn "Déjà absent : $(basename "$f")"
        fi
    done

    for f in \
        "$TERRAFORM_DIR/terraform.tfstate" \
        "$TERRAFORM_DIR/terraform.tfstate.backup"
    do
        if [[ -f "$f" ]]; then
            rm -f "$f"
            ok "Supprimé : terraform/$(basename "$f")"
        else
            warn "Déjà absent : terraform/$(basename "$f")"
        fi
    done

    for f in \
        "$ANSIBLE_DIR/inventory.ini" \
        "$ANSIBLE_DIR/group_vars/all.yml" \
        "$ANSIBLE_DIR/provision.yml" \
        "$ANSIBLE_DIR/restore.yml" \
        "$ANSIBLE_DIR/validate.yml"
    do
        if [[ -f "$f" ]]; then
            rm -f "$f"
            ok "Supprimé : ansible/$(basename "$f")"
        else
            warn "Déjà absent : ansible/$(basename "$f")"
        fi
    done

    CLOUD_HV_COUNT=0
    for f in "$ANSIBLE_DIR/host_vars"/*.yml; do
        [[ -f "$f" ]] || continue
        hv_basename=$(basename "$f")
        [[ "$hv_basename" =~ ^10\.0\. ]] && continue  # conserver les host_vars LXC
        rm -f "$f"
        CLOUD_HV_COUNT=$((CLOUD_HV_COUNT + 1))
    done
    if [[ $CLOUD_HV_COUNT -gt 0 ]]; then
        ok "Supprimé : $CLOUD_HV_COUNT fichier(s) ansible/host_vars/ (cloud)"
    else
        warn "Déjà absent : ansible/host_vars/ (cloud)"
    fi

    if [[ -d "$TERRAFORM_DIR/.terraform" ]]; then
        if confirmer "Supprimer aussi terraform/.terraform/ (cache provider, ~50 Mo) ?"; then
            rm -rf "$TERRAFORM_DIR/.terraform"
            rm -f "$TERRAFORM_DIR/.terraform.lock.hcl"
            ok "Supprimé : terraform/.terraform/"
        else
            warn "terraform/.terraform/ conservé."
        fi
    fi

else
    warn "Nettoyage local ignoré."
fi

# ─── Résumé ───────────────────────────────────────────────────────────────────

echo ""
echo "══════════════════════════════════════════"
echo "  Nettoyage terminé."
echo ""
echo "  Les containers LXC source sont intacts."
echo "  Pour relancer une migration propre : python3 src/orchestrator.py"
echo "══════════════════════════════════════════"
echo ""
