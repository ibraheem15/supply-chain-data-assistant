from pathlib import Path
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = ROOT / "envs"
AGENT_SQL_ENV_FILE = ENV_DIR / "agent_sqlserver.env"
VECTORDB_ENV_FILE = ENV_DIR / "vectorDB.env"
LLM_ENV_FILE = ENV_DIR / "LLM.env"

def load_settings(*env_files) -> dict[str,str]:
    settings = {}
    for env_file in env_files:
        settings.update(dotenv_values(env_file))
        # it returns a file's contents as a dict, so you don't need to parse lines by hand. It also handles quoted values and returns {}-like results for a missing file.
    return settings
