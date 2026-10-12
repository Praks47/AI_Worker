import shutil
import pytest
from fastapi.testclient import TestClient

from company_env import db
from company_env.faults import registry
from scripts import generate_data


@pytest.fixture(scope="session")
def _seed_template_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("seed_template")
    generate_data.generate(base / "data", base / "truth.json")
    return base / "data"


@pytest.fixture()
def client(tmp_path, monkeypatch, _seed_template_dir):
    data_dir = tmp_path / "data"
    shutil.copytree(_seed_template_dir, data_dir)
    monkeypatch.setenv("COMPANY_ENV_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COMPANY_ENV_ADMIN", "1")
    monkeypatch.delenv("COMPANY_ENV_DB", raising=False)
    monkeypatch.delenv("COMPANY_ENV_ENFORCE_UNIQUE", raising=False)
    registry.clear()
    from company_env.main import create_app
    with TestClient(create_app()) as c:
        yield c
    registry.clear()


NEW_INVOICE = {"vendor": "Wayne Enterprises", "invoice_number": "WE-2026-0412", "amount": 74250.00,
               "currency": "INR", "due_date": "2026-10-25", "source_document": "wayne_invoice_2026_09.pdf"}
