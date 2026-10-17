"""
External data source connectors — query user databases and use results like CSV.

Users connect to their own PostgreSQL / SQL Server / MongoDB / Snowflake / Redshift /
BigQuery / Databricks, run queries or
select tables, and the result set is loaded into the session as the active
dataset (same as after a CSV upload). Used for analysis, cleaning, charts, Q&A.
This is NOT for Stych's internal user/session storage (see database.* config).

- PostgreSQL: implemented (list tables, load table or custom query).
- SQL Server: WIP.
- MongoDB: WIP.
"""

from .bigquery import list_bigquery_tables, load_from_bigquery
from .databricks import list_databricks_tables, load_from_databricks
from .mongodb import list_mongodb_collections, load_from_mongodb  # WIP
from .postgres import (
    PostgresLoadError,
    build_postgres_connection_url,
    list_postgres_tables,
    load_from_postgres,
)
from .redshift import list_redshift_tables, load_from_redshift
from .snowflake import list_snowflake_tables, load_from_snowflake
from .sqlserver import list_sqlserver_tables, load_from_sqlserver  # WIP

__all__ = [
    "load_from_postgres",
    "list_postgres_tables",
    "build_postgres_connection_url",
    "PostgresLoadError",
    "load_from_sqlserver",
    "list_sqlserver_tables",
    "load_from_mongodb",
    "list_mongodb_collections",
    "load_from_redshift",
    "list_redshift_tables",
    "load_from_snowflake",
    "list_snowflake_tables",
    "load_from_bigquery",
    "list_bigquery_tables",
    "load_from_databricks",
    "list_databricks_tables",
]
