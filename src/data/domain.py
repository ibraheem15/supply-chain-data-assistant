import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
DOMAINS_DIR = ROOT / "domains"

@dataclass(frozen=True)
class Column:
    name: str
    legacy: str
    sql_type: str
    
@dataclass(frozen=True)
class Role:
    title: str
    intro: str
    rules: str
    evidence: tuple[str, ...]

@dataclass(frozen=True) # Immutable dataclass for domain configuration
class Domain:
    name: str
    table: str
    view: str
    csv: Path
    policy_dir: str
    columns: tuple[Column, ...]
    default_role: str
    roles: dict[str, "Role"]
    prompt: dict[str, str]

    
    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def legacy_names(self) -> dict[str, str]:
        return {column.name: column.legacy for column in self.columns}

    @property
    def sql_types(self) -> dict[str, str]:
        return {column.name: column.sql_type for column in self.columns}

def available_domains() -> list[str]:
    if not DOMAINS_DIR.is_dir():
        return []
    # looks inside domains folder and returns a sorted list of available domain names which have toml file
    # DOMAINS_DIR.iterdir() lists everything inside the domains directory
    return sorted(
        folder.name for folder in DOMAINS_DIR.iterdir() if (folder / "domain.toml").is_file()
    )
    
def load_domain(name: str | None = None) -> Domain | None:
    name = name or os.environ.get("DOMAIN", "logistics")
    domain_path = DOMAINS_DIR / name / "domain.toml"
    columns_path = DOMAINS_DIR / name / "columns.toml"
    roles_path = DOMAINS_DIR / name / "roles.toml"
    prompt_path = DOMAINS_DIR / name / "prompt.toml"
    
    if not domain_path.is_file():
        raise ValueError(f"Domain configuration for '{name}' not found.")
    if not columns_path.is_file():
        raise ValueError(f"Columns configuration for '{name}' not found.")
    if not roles_path.is_file():
        raise ValueError(f"Roles configuration for '{name}' not found.")
    if not prompt_path.is_file():
        raise ValueError(f"Prompt configuration for '{name}' not found.")
    with open(domain_path, "rb") as f:
        config = tomllib.load(f)
    with open(columns_path, "rb") as f:
        columns_config = tomllib.load(f)
    with open(roles_path, "rb") as f:
        roles_config = tomllib.load(f)
    with open(prompt_path, "rb") as f:
        prompt_config = tomllib.load(f)
        
    columns = tuple(
        Column(
            name=row["name"],
            legacy=row["legacy"],
            sql_type=row.get("sql_type", "FLOAT")
        )
        for row in columns_config["columns"]
    )
    
    roles = {
        role_id: Role(
            title=spec["title"],
            intro=spec["intro"],
            rules=spec["rules"],
            evidence=tuple(spec["evidence"]),
        )
        for role_id, spec in roles_config["roles"].items()
    }
    
    try:
        return Domain(
            name=config["name"],
            table=config["table"],
            view=config["view"],
            csv=ROOT / config["csv"],
            policy_dir=ROOT / config["policy_dir"],
            columns=columns,
            default_role=roles_config["default"],
            roles=roles,
            prompt=prompt_config,
        )
    except KeyError as e:
        raise ValueError(f"Missing required configuration key: {e}")
    
    