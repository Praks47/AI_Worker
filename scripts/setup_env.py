"""One-shot setup: generate documents + answer key, reset DB.   uv run python -m scripts.setup_env"""
from company_env import config
from scripts import reset_db  # noqa: F401  (reset_db runs on import guard below)
from company_env import db
from scripts import generate_data

if __name__ == "__main__":
    m = generate_data.generate()
    conn = db.connect()
    db.reset_db(conn)
    conn.close()
    print(f"{len(m['documents'])} documents written to {config.documents_dir()}")
    print(f"Database seeded at {config.db_path()}")
    print("Answer key written to eval/ground_truth.json (keep it away from the agent)")
