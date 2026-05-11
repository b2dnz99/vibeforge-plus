#!/bin/bash
# VibeForge+ Forgejo Bootstrap & Recovery Toolkit (VF-232)
#
# Run from the VM:
#   /opt/vibeforge/scripts/bootstrap_forgejo.sh <subcommand>
#
# Or from host:
#   ssh vibeforge "/opt/vibeforge/scripts/bootstrap_forgejo.sh <subcommand>"
#
# Subcommands:
#   bootstrap            Fresh install (refuses if already done)
#   reset-admin          Regenerate admin password
#   reset-service-token  Issue new service account API token
#   verify               Read-only health and auth check
#
# Park location: /opt/vibeforge/.bootstrap/forgejo.json (chmod 600)

if [ -z "$1" ]; then
    echo "Usage: $0 {bootstrap|reset-admin|reset-service-token|verify}"
    exit 1
fi

cd /opt/vibeforge && python3 scripts/bootstrap_forgejo.py "$1"
