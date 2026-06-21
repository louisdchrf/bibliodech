import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////app/data/bibliodech.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db():
    from app import models  # noqa: F401
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}

        # ── Migration colonnes books ───────────────────────────────────────────
        book_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(books)"))]
        if "enrichment_status" not in book_cols:
            conn.execute(text("ALTER TABLE books ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'ok'"))
        if "location_id" not in book_cols:
            conn.execute(text("ALTER TABLE books ADD COLUMN location_id INTEGER"))
        if "room_id" not in book_cols:
            conn.execute(text("ALTER TABLE books ADD COLUMN room_id INTEGER"))
            # Backfill : récupérer la pièce depuis la shelf existante
            conn.execute(text("""
                UPDATE books SET room_id = (
                    SELECT s.room_id FROM shelves s WHERE s.id = books.location_id
                ) WHERE location_id IS NOT NULL AND room_id IS NULL
            """))

        if "source_data" not in book_cols:
            conn.execute(text("ALTER TABLE books ADD COLUMN source_data TEXT"))
        if "enrichment_source" not in book_cols:
            conn.execute(text("ALTER TABLE books ADD COLUMN enrichment_source TEXT"))
            # Backfill : première clé de source_data comme source principale
            conn.execute(text("""
                UPDATE books SET enrichment_source = (
                    SELECT key FROM json_each(source_data) LIMIT 1
                ) WHERE source_data IS NOT NULL AND json_valid(source_data)
            """))

        user_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)"))]
        if "must_change_password" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"))
        if "email" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))

        if "app_logs" not in tables:
            conn.execute(text("""
                CREATE TABLE app_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL DEFAULT 'info',
                    category TEXT NOT NULL DEFAULT 'system',
                    message TEXT NOT NULL,
                    detail TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_app_logs_created_at ON app_logs (created_at)"))

        conn.commit()

        # ── Migration localisations plates → hiérarchie sites/rooms/shelves ──
        if "locations" in tables:
            # Vérifier si la migration a déjà eu lieu (shelves peuplées)
            already_migrated = conn.execute(text("SELECT COUNT(*) FROM shelves")).scalar() > 0
            if not already_migrated:
                old_locs = conn.execute(text("SELECT id, site, room, shelf FROM locations")).fetchall()
                site_map = {}   # name → id
                room_map = {}   # (site_id, name) → id
                loc_to_shelf = {}  # old location.id → new shelf.id

                for (loc_id, site_val, room_val, shelf_val) in old_locs:
                    site_name = site_val or "Sans localisation"
                    if site_name not in site_map:
                        conn.execute(text("INSERT INTO sites (name) VALUES (:n)"), {"n": site_name})
                        site_map[site_name] = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                    site_id = site_map[site_name]

                    room_name = room_val or "Sans pièce"
                    rk = (site_id, room_name)
                    if rk not in room_map:
                        conn.execute(text("INSERT INTO rooms (name, site_id) VALUES (:n, :s)"), {"n": room_name, "s": site_id})
                        room_map[rk] = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                    room_id = room_map[rk]

                    shelf_name = shelf_val or "Sans étagère"
                    conn.execute(text("INSERT INTO shelves (name, room_id) VALUES (:n, :r)"), {"n": shelf_name, "r": room_id})
                    loc_to_shelf[loc_id] = conn.execute(text("SELECT last_insert_rowid()")).scalar()

                for old_id, new_id in loc_to_shelf.items():
                    conn.execute(text("UPDATE books SET location_id = :new WHERE location_id = :old"),
                                 {"new": new_id, "old": old_id})
                conn.commit()

        # ── Migration shelf texte → shelves (installation sans ancien locations) ─
        if "locations" not in tables:
            shelves_text = conn.execute(text(
                "SELECT DISTINCT shelf FROM books WHERE shelf IS NOT NULL AND shelf != '' AND location_id IS NULL"
            )).fetchall()
            if shelves_text:
                # Site par défaut
                conn.execute(text("INSERT OR IGNORE INTO sites (id, name) VALUES (1, 'Maison')"))
                conn.execute(text("INSERT OR IGNORE INTO rooms (id, name, site_id) VALUES (1, 'Pièce principale', 1)"))
                for (sv,) in shelves_text:
                    conn.execute(text("INSERT INTO shelves (name, room_id) VALUES (:n, 1)"), {"n": sv})
                    shelf_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                    conn.execute(text("UPDATE books SET location_id = :lid WHERE shelf = :s AND location_id IS NULL"),
                                 {"lid": shelf_id, "s": sv})
                conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
