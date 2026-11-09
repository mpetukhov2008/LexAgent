"""Provision an isolated localhost PostgreSQL cluster using installed PG binaries.

Does not alter any existing Windows service, PostgreSQL cluster or system trust store.
"""

import os
import secrets
import ssl
import subprocess
from pathlib import Path

import httpx
import psycopg
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def main():
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    env_file = ROOT / ".env"
    if not env_file.exists():
        env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    values = dotenv_values(env_file)
    pg_bin = Path(os.getenv("PG_BIN", r"C:\Program Files\PostgreSQL\17\bin"))
    cluster = data / "pg"
    if not (cluster / "PG_VERSION").exists():
        if values.get("DATABASE_URL"):
            raise SystemExit(
                "DATABASE_URL is already configured. Use that database; no cluster created."
            )
        password = secrets.token_hex(24)
        password_file = data / "pg-password.tmp"
        password_file.write_text(password, encoding="ascii")
        try:
            subprocess.run(
                [
                    str(pg_bin / "initdb.exe"),
                    "-D",
                    str(cluster),
                    "-U",
                    "prospects",
                    "--pwfile=" + str(password_file),
                    "-A",
                    "scram-sha-256",
                    "--encoding=UTF8",
                    "--locale=C",
                ],
                check=True,
            )
        finally:
            password_file.unlink(missing_ok=True)
        set_key(
            env_file, "DATABASE_URL", f"postgresql://prospects:{password}@127.0.0.1:55432/prospects"
        )
    status = subprocess.run(
        [str(pg_bin / "pg_ctl.exe"), "-D", str(cluster), "status"], capture_output=True
    )
    if status.returncode:
        subprocess.run(
            [
                str(pg_bin / "pg_ctl.exe"),
                "-D",
                str(cluster),
                "-l",
                str(data / "pg-server.log"),
                "-o",
                "-p 55432 -h 127.0.0.1",
                "start",
            ],
            check=True,
        )
    url = dotenv_values(env_file)["DATABASE_URL"]
    with psycopg.connect(url.rsplit("/", 1)[0] + "/postgres", autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_database WHERE datname='prospects'").fetchone():
            db.execute("CREATE DATABASE prospects")
    ca = data / "gigachat-ca.pem"
    if not ca.exists():
        response = httpx.get(
            "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt", timeout=30
        )
        response.raise_for_status()
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=response.text)
        ca.write_bytes(response.content)
    print("PostgreSQL ready on localhost:55432. Local GigaChat CA ready. Fill API keys in .env.")


if __name__ == "__main__":
    main()
