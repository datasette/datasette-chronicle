import sqlite3, random, sqlite_chronicle

random.seed(42)

conn = sqlite3.connect("/tmp/timeline_test.db")
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=OFF")

# Drop everything for a clean slate
for name in ["articles", "comments", "users", "tags"]:
    conn.executescript(f"""
        DROP TABLE IF EXISTS _chronicle_{name};
        DROP TABLE IF EXISTS {name};
        DROP TRIGGER IF EXISTS chronicle_{name}_bi;
        DROP TRIGGER IF EXISTS chronicle_{name}_ai;
        DROP TRIGGER IF EXISTS chronicle_{name}_au;
        DROP TRIGGER IF EXISTS chronicle_{name}_ad;
    """)
conn.execute("DROP TABLE IF EXISTS _chroniclesnapshots")
conn.commit()

# Create tables
conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, body TEXT, author TEXT, status TEXT, views INTEGER)")
conn.execute("CREATE TABLE comments (id INTEGER PRIMARY KEY, article_id INTEGER, content TEXT, author TEXT)")
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, bio TEXT)")
conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, slug TEXT)")
conn.commit()

# Enable chronicle on all tables
for name in ["articles", "comments", "users", "tags"]:
    sqlite_chronicle.enable_chronicle(conn, name)

print("Chronicle enabled on:", sqlite_chronicle.list_chronicled_tables(conn))

# Now insert data WITHOUT triggers (direct chronicle inserts) to fake timestamps
# We'll disable triggers, insert data, then manually populate chronicle with realistic timestamps
# Actually: insert via triggers normally but then UPDATE chronicle timestamps to fake past dates

import time
NOW_MS = int(time.time() * 1000)
DAY_MS = 86400 * 1000
MONTH_MS = 30 * DAY_MS

authors = ["alice", "bob", "carol", "dave", "eve"]
statuses = ["draft", "published", "archived"]
tags = [("Python", "python"), ("SQLite", "sqlite"), ("Web", "web"), ("API", "api"), ("Database", "database"),
        ("Performance", "performance"), ("Security", "security"), ("Testing", "testing")]

# Insert tags (rarely updated)
for i, (name, slug) in enumerate(tags, 1):
    conn.execute("INSERT INTO tags (id, name, slug) VALUES (?, ?, ?)", (i, name, slug))
conn.commit()

# Insert 500 users
for i in range(1, 501):
    conn.execute("INSERT INTO users (id, name, email, bio) VALUES (?, ?, ?, ?)",
                 (i, f"User {i}", f"user{i}@example.com", f"Bio for user {i} " * 5))
conn.commit()

# Insert 5000 articles with varied content
for i in range(1, 5001):
    conn.execute("INSERT INTO articles (id, title, body, author, status, views) VALUES (?, ?, ?, ?, ?, ?)",
                 (i, f"Article {i}: {random.choice(['Deep dive into', 'Introduction to', 'Advanced', 'Getting started with'])} {random.choice(['Python', 'SQLite', 'Web APIs', 'Databases'])}",
                  f"Content for article {i}. " * 20,
                  random.choice(authors), random.choice(statuses), random.randint(0, 10000)))
conn.commit()

# Insert 20000 comments
for i in range(1, 20001):
    conn.execute("INSERT INTO comments (id, article_id, content, author) VALUES (?, ?, ?, ?)",
                 (i, random.randint(1, 5000),
                  f"Comment {i}: Great article! " * random.randint(1, 5),
                  random.choice(authors)))
conn.commit()

print("Data inserted. Now faking timestamps...")

# Now update chronicle timestamps to simulate a month of activity
# We want realistic patterns:
# - Most recent activity in the last few days
# - Bursts of activity at certain times
# - Some tables more active than others

def fake_chronicle_timestamps(conn, table_name, num_rows, now_ms, month_ms):
    """Assign fake timestamps to chronicle rows to simulate past activity."""
    rows = conn.execute(f"SELECT rowid FROM _chronicle_{table_name} ORDER BY rowid").fetchall()
    
    # Generate timestamps: exponential distribution weighted toward recent
    # More recent rows get more recent timestamps (higher version = more recent)
    timestamps = []
    for idx, (rowid,) in enumerate(rows):
        # Fraction through the month (0=oldest, 1=newest)
        frac = idx / max(len(rows) - 1, 1)
        
        # Add noise and clustering
        # Create "burst" events - clusters of activity
        burst_center = random.choice([0.7, 0.8, 0.85, 0.9, 0.95, 1.0])
        burst_strength = random.random() * 0.3
        frac = frac * (1 - burst_strength) + burst_center * burst_strength
        frac = min(1.0, max(0.0, frac + random.gauss(0, 0.02)))
        
        ms_ago = int((1.0 - frac) * month_ms)
        ts = now_ms - ms_ago
        timestamps.append((ts, rowid))
    
    # Sort so higher rowid = more recent (mostly)
    timestamps.sort(key=lambda x: x[0])
    ts_by_rowid = {rowid: ts for ts, rowid in zip([t[0] for t in timestamps], [r[0] for r in rows])}
    
    conn.executemany(
        f"UPDATE _chronicle_{table_name} SET __added_ms = ?, __updated_ms = ?, __version = ? WHERE rowid = ?",
        [(ts_by_rowid[rowid], ts_by_rowid[rowid], version+1, rowid)
         for version, (rowid,) in enumerate(rows)]
    )

fake_chronicle_timestamps(conn, "tags", len(tags), NOW_MS, MONTH_MS)
fake_chronicle_timestamps(conn, "users", 500, NOW_MS, MONTH_MS)
fake_chronicle_timestamps(conn, "articles", 5000, NOW_MS, MONTH_MS)
fake_chronicle_timestamps(conn, "comments", 20000, NOW_MS, MONTH_MS)

# Add some recent updates (simulating rows updated after initial insert)
# Update 200 articles recently (last 3 days)
for article_id in random.sample(range(1, 5001), 200):
    days_ago = random.random() * 3
    ts = NOW_MS - int(days_ago * DAY_MS)
    conn.execute("UPDATE _chronicle_articles SET __updated_ms = ?, __version = __version + 50000 WHERE id = ?",
                 (ts, article_id))

# Update 50 users in last week
for user_id in random.sample(range(1, 501), 50):
    days_ago = random.random() * 7
    ts = NOW_MS - int(days_ago * DAY_MS)
    conn.execute("UPDATE _chronicle_users SET __updated_ms = ?, __version = __version + 30000 WHERE id = ?",
                 (ts, user_id))

conn.commit()
print("Timestamps faked successfully!")

# Show statistics
for table in ["articles", "comments", "users", "tags"]:
    row_count = conn.execute(f"SELECT COUNT(*) FROM _chronicle_{table}").fetchone()[0]
    min_ts, max_ts = conn.execute(f"SELECT MIN(__updated_ms), MAX(__updated_ms) FROM _chronicle_{table}").fetchone()
    span_days = (max_ts - min_ts) / DAY_MS
    print(f"  {table}: {row_count} chronicle rows, spanning {span_days:.1f} days")

