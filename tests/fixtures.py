import json
import os
import sqlite3
from unittest.mock import patch

# Do not inspect an installed DingTalk account during automated tests.
with patch.dict(os.environ, {"DINGTALK_UID": "1", "DINGTALK_DATA_DIR": os.path.dirname(__file__)}):
    import config
    import parser


def create_database(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tbconversation (
            cid TEXT PRIMARY KEY, type INTEGER, title TEXT, memberCount INTEGER DEFAULT 2,
            createAt INTEGER DEFAULT 0, lastModify INTEGER DEFAULT 100,
            unreadCount INTEGER DEFAULT 0, top INTEGER DEFAULT 0, ownerId INTEGER DEFAULT 1,
            isNotification INTEGER DEFAULT 0, extension TEXT DEFAULT '', status INTEGER DEFAULT 1
        );
        CREATE TABLE tbuser_profile_v2 (
            uid INTEGER PRIMARY KEY, nick TEXT, realName TEXT,
            iconMediaId TEXT, mobile TEXT, email TEXT
        );
        INSERT INTO tbuser_profile_v2 (uid, nick, realName) VALUES
            (1, 'Me', 'My Name'), (2, 'Alice', 'Alice Real'),
            (3, '', 'Bob'), (4, 'Alice Team', 'Other Name');
        INSERT INTO tbconversation (cid, type, title) VALUES
            ('1:2', 1, ''), ('3:1', 1, 'old title'), ('1:4', 1, ''),
            ('1:99', 1, 'Fallback'), ('group', 2, 'Project Group');
        INSERT INTO tbconversation (cid, type, title, status) VALUES
            ('hidden', 2, 'Alice hidden', 0);
    """)
    for table in ("tbmsg_000", "tbmsg_127"):
        conn.execute(f'''CREATE TABLE "{table}" (
            primaryKey INTEGER PRIMARY KEY, cid TEXT, localId INTEGER, mid INTEGER,
            senderId INTEGER DEFAULT 2, type INTEGER DEFAULT 1, creatorType INTEGER DEFAULT 0,
            createdAt INTEGER, lastModify INTEGER DEFAULT 0, contentType INTEGER DEFAULT 1,
            content TEXT, recallStatus INTEGER DEFAULT 0, atIds TEXT DEFAULT '',
            attachments TEXT DEFAULT '', extension TEXT DEFAULT '',
            readStatus INTEGER DEFAULT 0, sentlocaltime INTEGER DEFAULT 0
        )''')
    # More than 500 rows, tied timestamps, and a short final page.
    conn.executemany(
        'INSERT INTO tbmsg_000 (mid, cid, createdAt, content) VALUES (?, ?, ?, ?)',
        [(i, "1:2", i // 2, json.dumps({"text": f"needle {i}"})) for i in range(1, 555)],
    )
    # Newest matches live in the last shard. The same timestamp and mid can
    # occur in different conversations, so ordering must include a stable tie-breaker.
    conn.executemany(
        'INSERT INTO tbmsg_127 (mid, cid, createdAt, content) VALUES (?, ?, ?, ?)',
        [(i, "group", 1000 + i // 2, json.dumps({"text": f"needle {i}"})) for i in range(1, 206)],
    )
    conn.execute("INSERT INTO tbmsg_127 (mid, cid, createdAt, content, recallStatus) VALUES (999, 'group', 9999, 'needle recalled', 1)")
    conn.commit()
    return conn
