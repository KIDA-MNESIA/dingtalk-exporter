"""Launch the real stdio server against an explicitly supplied test snapshot."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
database = Path(sys.argv[1]).resolve()
uid = sys.argv[2] if len(sys.argv) > 2 else "1"
os.environ["DINGTALK_UID"] = uid
os.environ["DINGTALK_DATA_DIR"] = str(database.parent)

import config

config.DECRYPTED_DB_PATH = str(database)

from mcp_server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
