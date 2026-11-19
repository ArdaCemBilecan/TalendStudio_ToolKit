"""Generate a small, synthetic Talend *project* that shows what the dependency explorer is for.

Layout (a Studio-like project folder):

    talend_demo/
      talend.project
      metadata/connections/ODS_DB_0.1.{item,properties}   <- reusable DB connections, found by name
      metadata/connections/AKY_DB_0.1.{item,properties}
      process/job_master_daily_0.1.*  -> starts stage_customers, stage_orders, publish_marts (OnSubjobOk)
      process/job_master_weekly_0.1.* -> starts stage_orders and export_sales
      process/job_export_sales, job_audit_daily            (not started by job_master_daily)

``job_master_daily`` and everything it starts use ODS_DB exactly 10 times and AKY_DB exactly 2 times.
Everything is invented: tables, hosts and names. Usage: python tools/generate_demo.py --out samples/talend_demo
"""

from __future__ import annotations

import argparse
from pathlib import Path

from generate_samples import JobBuilder, delp, eid, job_id, properties_xml, q, setp  # noqa: F401

PROJECT = "DEMO"
STAMP = "2026-01-01T00:00:00.000+0000"
AUTHOR = "_AUTHOR_PLACEHOLDER0000"
CONNECTIONS = {"ODS_DB": "ods-db.example.local", "AKY_DB": "aky-db.example.local"}

# job -> list of (connection, SQL). Every statement is one tOracleRow bound to a repository connection.
SQL_JOBS: dict[str, list[tuple[str, str]]] = {
    "job_stage_customers": [
        ("ODS_DB", "TRUNCATE TABLE STG.CUSTOMERS"),
        ("ODS_DB", "INSERT INTO STG.CUSTOMERS (customer_id, name) SELECT customer_id, name FROM ODS.CUSTOMERS"),
        ("ODS_DB", "INSERT INTO STG.CUSTOMER_ADDRESS SELECT customer_id, address FROM ODS.ADDRESSES"),
    ],
    "job_stage_orders": [
        ("ODS_DB", "TRUNCATE TABLE STG.ORDERS"),
        ("ODS_DB", "INSERT INTO STG.ORDERS SELECT * FROM ODS.ORDERS"),
        ("ODS_DB", "INSERT INTO STG.ORDER_LINES SELECT * FROM ODS.ORDER_LINES"),
        ("ODS_DB", "INSERT INTO STG.PAYMENTS SELECT * FROM ODS.PAYMENTS"),
        ("AKY_DB", "INSERT INTO STG.FX_RATES SELECT * FROM AKY.FX_RATES"),
    ],
    "job_publish_marts": [
        ("ODS_DB", "INSERT INTO MART.SALES SELECT o.order_id, c.name FROM STG.ORDERS o JOIN STG.CUSTOMERS c "
                   "ON c.customer_id = o.customer_id"),
        ("ODS_DB", "INSERT INTO MART.SALES_LINES SELECT l.* FROM STG.ORDER_LINES l JOIN STG.ORDERS o "
                   "ON o.order_id = l.order_id"),
        ("ODS_DB", "INSERT INTO MART.PAYMENT_SUMMARY SELECT order_id, SUM(amount) FROM STG.PAYMENTS GROUP BY order_id"),
        ("AKY_DB", "INSERT INTO MART.SALES_EUR SELECT s.*, f.rate FROM MART.SALES s JOIN STG.FX_RATES f "
                   "ON f.currency = 'EUR'"),
    ],
    "job_export_sales": [
        ("ODS_DB", "INSERT INTO EXPORT.SALES_FEED SELECT * FROM MART.SALES"),
    ],
    "job_audit_daily": [
        ("ODS_DB", "INSERT INTO AUDIT.DAILY SELECT * FROM ODS.AUDIT_LOG"),
    ],
}
MASTERS = {
    "job_master_daily": ["job_stage_customers", "job_stage_orders", "job_publish_marts"],
    "job_master_weekly": ["job_stage_orders", "job_export_sales"],
}


def sql_job(name: str, statements: list[tuple[str, str]]) -> JobBuilder:
    jb = JobBuilder(name, PROJECT)
    prev = None
    for i, (conn, sql) in enumerate(statements, start=1):
        uid = f"tOracleRow_{i}"
        el = jb._add("tOracleRow", uid, 140 + (i - 1) * 190, 160, f"{conn.lower()}_{i}")
        setp(el, "PROPERTY:PROPERTY_TYPE", "REPOSITORY", "TECHNICAL")
        setp(el, "PROPERTY:REPOSITORY_PROPERTY_TYPE", eid("connection", conn), "TECHNICAL")
        setp(el, "QUERY", q(sql), "MEMO_SQL")
        if prev:
            jb.on_ok(prev, uid)
        prev = uid
    return jb


def master_job(name: str, children: list[str]) -> JobBuilder:
    jb = JobBuilder(name, PROJECT)
    prev = None
    for i, child in enumerate(children, start=1):
        uid = f"tRunJob_{i}"
        jb.run_job(uid, 140 + (i - 1) * 220, 160, child)
        if prev:
            jb.on_ok(prev, uid)
        prev = uid
    return jb


def connection_properties(name: str) -> str:
    a = lambda k: eid("conn-props", name, k)  # noqa: E731
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xmi:XMI xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">
  <TalendProperties:Property xmi:id="{a('property')}" id="{eid('connection', name)}" label="{name}" version="0.1" statusCode="" item="{a('item')}" displayName="{name}">
    <author href="../../talend.project#{AUTHOR}"/>
    <additionalProperties xmi:id="{a('c1')}" key="created_product_fullname" value="Talend Big Data Platform"/>
    <additionalProperties xmi:id="{a('c2')}" key="created_product_version" value="8.0.1"/>
    <additionalProperties xmi:id="{a('c3')}" key="created_date" value="{STAMP}"/>
  </TalendProperties:Property>
  <TalendProperties:ItemState xmi:id="{a('state')}" path=""/>
  <TalendProperties:DatabaseConnectionItem xmi:id="{a('item')}" property="{a('property')}" state="{a('state')}" typeName="Oracle OCI">
    <connection href="{name}_0.1.item#{a('conn')}"/>
  </TalendProperties:DatabaseConnectionItem>
</xmi:XMI>
"""


def connection_item(name: str, host: str) -> str:
    """A minimal stand-in for a Studio DatabaseConnection item: enough for tmt (it reads only the .properties label),
    NOT verified to import into Studio. No credentials."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TalendMetadata:DatabaseConnection xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendMetadata="http://www.talend.org/metadata/connection/2010" xmi:id="{eid('conn-props', name, 'conn')}" name="{name}" id="{eid('connection', name)}" label="{name}" DatabaseType="Oracle OCI" DriverClass="oracle.jdbc.OracleDriver" URL="jdbc:oracle:thin:@//{host}:1521/{name}" Port="1521" Username="demo_user" Password="" ServerName="{host}" SID="{name}" DbmsId="oracle_id" ProductId="ORACLE"/>
"""


def write(out: Path) -> list[Path]:
    written: list[Path] = []
    (out / "process").mkdir(parents=True, exist_ok=True)
    (out / "metadata" / "connections").mkdir(parents=True, exist_ok=True)
    project_file = out / "talend.project"
    project_file.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xmi:XMI xmi:version="2.0" xmlns:xmi="http://www.omg.org/XMI" xmlns:TalendProperties="http://www.talend.org/properties">\n'
        f'  <TalendProperties:Project xmi:id="{eid("project")}" label="Demo" description="Synthetic demo project" language="java" '
        f'technicalLabel="{PROJECT}" local="true" productVersion="Talend Big Data Platform-8.0.1" type="DQ"/>\n</xmi:XMI>\n',
        encoding="utf-8", newline="\n")
    written.append(project_file)
    for name, host in CONNECTIONS.items():
        for ext, text in (("properties", connection_properties(name)), ("item", connection_item(name, host))):
            p = out / "metadata" / "connections" / f"{name}_0.1.{ext}"
            p.write_text(text, encoding="utf-8", newline="\n")
            written.append(p)
    jobs = {n: sql_job(n, s) for n, s in SQL_JOBS.items()}
    jobs |= {n: master_job(n, c) for n, c in MASTERS.items()}
    for name, jb in sorted(jobs.items()):
        item = out / "process" / f"{name}_0.1.item"
        props = out / "process" / f"{name}_0.1.properties"
        item.write_bytes(jb.build())
        props.write_text(properties_xml(name, AUTHOR, STAMP), encoding="utf-8", newline="\n")
        written += [item, props]
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    for p in write(Path(ap.parse_args().out)):
        print("wrote", p.name)


if __name__ == "__main__":
    main()
