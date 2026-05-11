#!/usr/bin/env python3
"""
Dev/UAT SA password seeder — sets the super_admin password to a known dev value.

DEV/UAT ONLY. Never run against prod.

Usage:
    docker exec vibeforge-app-1 python scripts/seed_dev_sa.py [password]

Defaults to '1234' if no password supplied. Refuses to run if VIBEFORGE_ENV=prod.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import bcrypt


def main():
    env = os.environ.get("VIBEFORGE_ENV", "").lower()
    if env == "prod":
        print("ERROR: refusing to run against prod (VIBEFORGE_ENV=prod). Dev/UAT only.")
        sys.exit(2)

    password = sys.argv[1] if len(sys.argv) > 1 else "1234"

    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        from app.models.user import User
        sa = db.query(User).filter(User.role == "super_admin").first()
        if not sa:
            print("ERROR: No super_admin user found. Run alembic + initial bootstrap first.")
            sys.exit(1)

        sa.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db.commit()
        print(f"OK: SA '{sa.email}' password set to '{password}' (env={env or 'unset'})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
