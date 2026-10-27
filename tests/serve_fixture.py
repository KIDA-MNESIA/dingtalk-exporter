"""Launch the real stdio server against an explicitly supplied test snapshot."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
database = Path(sys.argv[1]).resolve()
uid = sys.argv[2] if len(sys.argv) > 2 else "1"
os.environ["DINGTALK_UID"] = uid
os.environ["DINGTALK_DATA_DIR"] = sys.argv[3] if len(sys.argv) > 3 else str(database.parent)

import config

config.DECRYPTED_DB_PATH = str(database)
config.DATA_DIR = str(database.parent / "runtime")
config.DECRYPTED_DIR = str(database.parent / "decrypted")
config.EXPORT_DIR = str(database.parent / "exports")
config.SYNC_STATE_FILE = str(database.parent / "runtime" / "sync_state.json")

from mcp_server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
