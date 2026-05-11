#!/usr/bin/env python3
"""
Dev/UAT account seeder. DEV/UAT ONLY.

Seeds six accounts:
    sa@<env>.local   super_admin    SA      pw=1234
    su@<env>.local   super_user     SU      pw=1234
    po@<env>.local   user           PO      pw=1234
    pu@<env>.local   user           PU      pw=1234
    pv@<env>.local   viewer         PV      pw=1234
    pkhan (preserve) super_user     -       pw=<randomized unknown>

Roles are strictly the documented enum: super_admin, super_user, user, viewer.
Non-standard roles (project_owner/project_user/project_viewer) are MIGRATED AWAY:
existing DB rows with those values get remapped to the closest standard role
(project_owner/project_user → user, project_viewer → viewer).

pkhan is preserved (not deleted) but his password is rotated to a random unknown
value so DEV/UAT doesn't accidentally authenticate as PK by muscle memory. Reset
via /admin if PK wants to log in as himself on DEV/UAT.

Usage (inside container):
    python scripts/seed_dev_accounts.py --env dev
    python scripts/seed_dev_accounts.py --env uat

Refuses any other --env value.
"""
import argparse
import os
import secrets
import string
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import bcrypt

PASSWORD = "1234"
ACCOUNTS = [
    ("sa", "super_admin", "SA"),
    ("su", "super_user",  "SU"),
    ("po", "user",        "PO"),
    ("pu", "user",        "PU"),
    ("pv", "viewer",      "PV"),
]
ROLE_MIGRATION = {
    "project_owner":  "user",
    "project_user":   "user",
    "project_viewer": "viewer",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--env", required=True, choices=["dev", "uat"],
                   help="Target environment. 'prod' is explicitly not accepted.")
    return p.parse_args()


def main():
    args = parse_args()
    env = args.env

    db_url = os.environ.get("DATABASE_URL",
                            "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    pw_hash_known = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    domain = f"{env}.local"
    now = datetime.now(timezone.utc)

    try:
        from app.models.user import User

        migrated = 0
        for old_role, new_role in ROLE_MIGRATION.items():
            rows = db.query(User).filter(User.role == old_role).all()
            for r in rows:
                r.role = new_role
                migrated += 1
        if migrated:
            print(f"[migrate] {migrated} user(s) moved from non-standard roles to standard")

        for username, role, display in ACCOUNTS:
            email = f"{username}@{domain}"
            u = db.query(User).filter(User.username == username).first()
            if u:
                u.email = email
                u.display_name = display
                u.role = role
                u.status = "active"
                u.password_hash = pw_hash_known
                u.must_change_password = False
                u.deleted_at = None
                u.deleted_by = None
                print(f"[reset ] {username:<8} role={role:<12} email={email}  pw=1234")
            else:
                u = User(
                    id=str(uuid.uuid4()),
                    username=username,
                    email=email,
                    display_name=display,
                    role=role,
                    status="active",
                    password_hash=pw_hash_known,
                    must_change_password=False,
                    created_at=now,
                    updated_at=now,
                )
                db.add(u)
                print(f"[create] {username:<8} role={role:<12} email={email}  pw=1234")

        alphabet = string.ascii_letters + string.digits
        random_pw = "".join(secrets.choice(alphabet) for _ in range(32))
        pk = db.query(User).filter(User.username == "pkhan").first()
        if pk:
            pk.password_hash = bcrypt.hashpw(random_pw.encode(), bcrypt.gensalt()).decode()
            pk.must_change_password = False
            pk.status = "active"
            print(f"[rotate] pkhan     role={pk.role}  pw=<random, unknown>")
        else:
            print("[skip  ] pkhan not present on this env; no rotation needed")

        db.commit()
        print()
        print(f"Seed complete on {env}. Short-user creds: all use password '1234'.")
        print("pkhan password on this env is a random unknown; reset via /admin if needed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
