#!/bin/bash
# VibeForge+ Super Admin Password Reset
# Run from the VM: ./scripts/reset_sa.sh
# Or from host:    ssh vibeforge "/opt/vibeforge/scripts/reset_sa.sh"
cd /opt/vibeforge && docker compose exec app python scripts/reset_sa_password.py
