import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////app/data/bibliodech.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db():
    import secrets as _secrets
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
        if "avatar" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN avatar TEXT"))

        if "series_proposals" not in tables:
            conn.execute(text("""
                CREATE TABLE series_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_ids TEXT NOT NULL,
                    proposed_name TEXT,
                    signal TEXT NOT NULL,
                    existing_series_id INTEGER REFERENCES series(id) ON DELETE SET NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))

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

        # ── Migration colonnes series ─────────────────────────────────────────
        series_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(series)"))]
        if "bnf_max_known" not in series_cols:
            conn.execute(text("ALTER TABLE series ADD COLUMN bnf_max_known INTEGER"))

        # ── Migration colonnes discs ──────────────────────────────────────────
        if "discs" in tables:
            disc_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(discs)"))]
            if "location_id" not in disc_cols:
                conn.execute(text("ALTER TABLE discs ADD COLUMN location_id INTEGER REFERENCES shelves(id)"))

        # ── Index de performance (idempotents via IF NOT EXISTS) ─────────────
        existing_idx = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ))}
        perf_indexes = [
            ("ix_books_series_id",          "CREATE INDEX IF NOT EXISTS ix_books_series_id ON books (series_id)"),
            ("ix_books_room_id",             "CREATE INDEX IF NOT EXISTS ix_books_room_id ON books (room_id)"),
            ("ix_books_enrichment_status",   "CREATE INDEX IF NOT EXISTS ix_books_enrichment_status ON books (enrichment_status)"),
            ("ix_series_proposals_status",   "CREATE INDEX IF NOT EXISTS ix_series_proposals_status ON series_proposals (status)"),
        ]
        for idx_name, ddl in perf_indexes:
            if idx_name not in existing_idx:
                conn.execute(text(ddl))

        # ── Migration FK books.location_id : locations → shelves ─────────────
        books_fks = conn.execute(text("PRAGMA foreign_key_list(books)")).fetchall()
        bad_fk = any(row[2] == 'locations' and row[3] == 'location_id' for row in books_fks)
        if bad_fk:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("""
                CREATE TABLE books_new (
                    id INTEGER NOT NULL,
                    isbn VARCHAR,
                    title VARCHAR NOT NULL,
                    subtitle VARCHAR,
                    authors TEXT,
                    publisher VARCHAR,
                    publish_date VARCHAR,
                    cover_url VARCHAR,
                    description TEXT,
                    page_count INTEGER,
                    language VARCHAR,
                    source VARCHAR NOT NULL,
                    work_key VARCHAR,
                    series_id INTEGER REFERENCES series(id),
                    series_position FLOAT,
                    shelf VARCHAR,
                    added_at DATETIME,
                    enrichment_status TEXT NOT NULL DEFAULT 'ok',
                    location_id INTEGER REFERENCES shelves(id),
                    room_id INTEGER REFERENCES rooms(id),
                    source_data TEXT,
                    enrichment_source TEXT,
                    genre VARCHAR,
                    PRIMARY KEY (id),
                    CONSTRAINT uq_books_isbn UNIQUE (isbn)
                )
            """))
            conn.execute(text("INSERT INTO books_new SELECT * FROM books"))
            conn.execute(text("DROP TABLE books"))
            conn.execute(text("ALTER TABLE books_new RENAME TO books"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()

        # ── SECRET_KEY auto-générée au premier démarrage ─────────────────────
        existing_key = conn.execute(
            text("SELECT value FROM settings WHERE key='secret_key'")
        ).scalar()
        if not existing_key:
            generated = _secrets.token_hex(32)
            conn.execute(
                text("INSERT INTO settings (key, value) VALUES ('secret_key', :v)"),
                {"v": f'"{generated}"'},
            )

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
