"""Reset the finance database to its seed state.   uv run python -m scripts.reset_db"""
from company_env import db
from company_env import config

if __name__ == "__main__":
    conn = db.connect()
    db.reset_db(conn)
    conn.close()
    print(f"Database reset and seeded: {config.db_path()}")
