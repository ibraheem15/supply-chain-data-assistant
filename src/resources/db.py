# Every tool that touches SQL Server needs a connection. Rather than repeat the connection code, db.py holds it once.
# In system design, this is called single responsibility principle.

import pyodbc


def open_connection(settings, user_key="MSSQL_AGENT_USER", password_key="MSSQL_AGENT_PASSWORD"):
    connection_string = (
        f"DRIVER={{{settings['MSSQL_DRIVER']}}};"
        f"SERVER={settings['MSSQL_SERVER']};"
        f"DATABASE={settings['MSSQL_DATABASE']};"
        f"UID={settings[user_key]};"
        f"TrustServerCertificate=yes;"
        f"PWD={settings[password_key]}"
    )
    connection = pyodbc.connect(connection_string, timeout=10)
    connection.timeout = 15
    return connection
