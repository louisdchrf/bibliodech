"""
Fixtures partagées entre tous les tests.
Utilise une base SQLite en mémoire, isolée par test.
"""
import os
import pytest

# Forcer une base mémoire pour les tests — avant tout import de l'app
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_bibliodech.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("COVERS_DIR", "/tmp/test_covers")
os.environ.setdefault("AVATARS_DIR", "/tmp/test_avatars")


from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

TEST_DB_URL = "sqlite:///./test_bibliodech.db"

engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Crée le schéma une fois pour toute la session de tests."""
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    import os
    if os.path.exists("./test_bibliodech.db"):
        os.remove("./test_bibliodech.db")


@pytest.fixture
def db():
    """Session DB isolée, rollback après chaque test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSession(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    """TestClient FastAPI avec override de la dépendance DB."""
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(db):
    """TestClient authentifié en tant qu'admin."""
    from app.auth import hash_password
    from app.models import User
    from datetime import datetime

    user = User(
        username="testadmin",
        password_hash=hash_password("testpass"),
        role="admin",
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/login", data={"username": "testadmin", "password": "testpass"}, follow_redirects=False)
        assert r.status_code in (200, 302), f"Login failed: {r.status_code}"
        yield c
    app.dependency_overrides.clear()
