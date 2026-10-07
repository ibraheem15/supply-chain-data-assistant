# Every tool that touches SQL Server needs a connection. Rather than repeat the connection code, db.py holds it once.
# In system design, this is called single responsibility principle.

import pyodbc
from resources.settings import load_settings


def open_connection(settings: dict[str, str]):
    connection_string = (
        f"DRIVER={{{settings['MSSQL_DRIVER']}}};"
        f"SERVER={settings['MSSQL_SERVER']};"
        f"DATABASE={settings['MSSQL_DATABASE']};"
        f"UID={settings['MSSQL_AGENT_USER']};"
        f"TrustServerCertificate=yes;"
        f"PWD={settings['MSSQL_AGENT_PASSWORD']}"
    )
    connection = pyodbc.connect(connection_string, timeout=10)
    connection.timeout = 15
    return connection
