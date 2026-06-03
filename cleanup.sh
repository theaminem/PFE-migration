#!/bin/bash
# Nettoyage post-migration : supprime les ressources OpenStack et les artefacts locaux

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/terraform"
ANSIBLE_DIR="$SCRIPT_DIR/ansible"

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

# Supprime une ressource OpenStack par nom, silencieusement si absente
os_delete() {
    local type="$1"   # server / volume / keypair / security group
    local name="$2"
    if openstack "$type" delete "$name" 2>/dev/null; then
        ok "Supprimé : $type '$name'"
    else
        warn "Déjà absent ou erreur : $type '$name'"
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
echo "  Ressources ciblées par nom :"
echo "    • Instances    : instance-apache, instance-backup, instance-ftp,"
echo "                     instance-mariadb, instance-nfs"
echo "    • Volume       : mariadb-data"
echo "    • Keypair      : migration-key"
echo "    • Sec. groups  : sg-mariadb, sg-apache, sg-backup, sg-ftp, sg-nfs"

# Vérification prérequis
SKIP_CLOUD=0

if [[ -z "${OS_AUTH_URL:-}" || -z "${OS_USERNAME:-}" || -z "${OS_PASSWORD:-}" ]]; then
    fail "Variables d'environnement OpenStack manquantes (OS_AUTH_URL, OS_USERNAME, OS_PASSWORD)"
    echo "     Source ton fichier openrc avant de relancer ce script."
    SKIP_CLOUD=1
fi

if [[ $SKIP_CLOUD -eq 0 ]] && ! command -v openstack &>/dev/null; then
    fail "CLI 'openstack' introuvable dans le PATH"
    SKIP_CLOUD=1
fi

if [[ $SKIP_CLOUD -eq 0 ]]; then
    if confirmer "Supprimer toutes les ressources OpenStack de la migration ?"; then

        echo ""
        echo "  Suppression des instances (attente de leur arrêt)..."
        for name in instance-apache instance-backup instance-ftp instance-mariadb instance-nfs; do
            os_delete "server" "$name"
        done

        # Attendre que les instances soient bien supprimées avant de toucher aux SG
        echo ""
        echo "  Attente de la suppression complète des instances..."
        for name in instance-apache instance-backup instance-ftp instance-mariadb instance-nfs; do
            for i in $(seq 1 24); do
                if ! openstack server show "$name" &>/dev/null; then
                    break
                fi
                sleep 5
            done
        done
        ok "Instances supprimées"

        echo ""
        echo "  Suppression du volume..."
        os_delete "volume" "mariadb-data"

        echo ""
        echo "  Suppression de la keypair..."
        os_delete "keypair" "migration-key"

        echo ""
        echo "  Suppression des security groups..."
        for name in sg-mariadb sg-apache sg-backup sg-ftp sg-nfs; do
            os_delete "security group" "$name"
        done

        # Suppression explicite du réseau (OpenStack exige un ordre précis)
        echo ""
        echo "  Suppression du réseau (router → subnet → network)..."
        openstack router remove subnet migration-router migration-subnet 2>/dev/null && \
            ok "Interface router détachée" || warn "Interface router déjà absente"
        openstack router delete migration-router 2>/dev/null && \
            ok "Router supprimé" || warn "Router déjà absent"
        openstack network delete migration-net 2>/dev/null && \
            ok "Réseau supprimé" || warn "Réseau déjà absent"

        # Terraform destroy en complément pour purger le tfstate
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
        basename=$(basename "$f")
        [[ "$basename" =~ ^10\.0\. ]] && continue  # conserver les host_vars LXC
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
