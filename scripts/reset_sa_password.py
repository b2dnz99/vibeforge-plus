#!/usr/bin/env python3
"""
VibeForge+ Super Admin Password Reset

Run inside the app container:
    docker exec vibeforge-app-1 python scripts/reset_sa_password.py

Generates a random password, updates the SA user, prints the new password to stdout.
The SA must change this password immediately after login.
"""
import getpass
import json
import os
import secrets
import socket
import string
import sys
from datetime import datetime, timezone

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import bcrypt


def main():
    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        # Find the super_admin user
        from app.models.user import User
        sa = db.query(User).filter(User.role == "super_admin").first()

        if not sa:
            print("ERROR: No super_admin user found in database.")
            print("Has the auth migration been run? (alembic upgrade head)")
            sys.exit(1)

        # Generate a random password
        alphabet = string.ascii_letters + string.digits + "!@#$%"
        new_password = ''.join(secrets.choice(alphabet) for _ in range(16))

        # Hash and update
        sa.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()

        # VF-311: record an explicit audit event so the SA can see on next login that
        # a host-side reset happened. Without this, a force-reset is invisible from
        # the app. See 0-MD/0-Documentation/public/identity-roles.md §2 + bug 5.7.
        try:
            os_user = os.environ.get("USER") or os.environ.get("USERNAME") or getpass.getuser()
        except Exception:
            os_user = "unknown"
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"
        from app.models.activity import ActivityEvent
        details = {
            "script_path": os.path.abspath(__file__),
            "os_user": os_user,
            "hostname": hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": f"host:{os_user}@{hostname}",
        }
        db.add(ActivityEvent(
            project_id=None,
            task_id=None,
            actor_type="system",
            actor_user_id=sa.id,
            action="sa_password_force_reset",
            details=json.dumps(details),
        ))
        db.commit()

        print("=" * 50)
        print("  VibeForge+ Super Admin Password Reset")
        print("=" * 50)
        print()
        print(f"  User:     {sa.display_name} ({sa.email})")
        print(f"  Role:     {sa.role}")
        print(f"  Password: {new_password}")
        print()
        print("  CHANGE THIS PASSWORD IMMEDIATELY AFTER LOGIN")
        print("  Go to: Admin Panel > View SA account > Change Password")
        print()
        print("=" * 50)

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
