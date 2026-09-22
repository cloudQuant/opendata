"""
Initialize scheduled tasks from scripts and sync data warehouse tables.

Usage:
    python -m scripts.init_tasks_and_tables

This script:
1. Creates a daily scheduled task for each data_script that doesn't already have one
2. Syncs data warehouse tables into data_tables metadata
3. Refreshes row_count for all data_tables from the actual warehouse
"""

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, select, text

from opendata.core.config import settings
from opendata.core.database import async_session_maker, engine
from opendata.models.data_script import DataScript
from opendata.models.data_table import DataTable
from opendata.models.task import ScheduledTask, ScheduleType


async def create_tasks_from_scripts():
    """Create a daily scheduled task for each script that doesn't have one."""
    async with async_session_maker() as db:
        # Get all scripts
        result = await db.execute(select(DataScript).where(DataScript.is_active == True))
        scripts = result.scalars().all()
        print(f"Found {len(scripts)} active scripts")

        # Get existing tasks (by script_id)
        result = await db.execute(select(ScheduledTask.script_id))
        existing_script_ids = {row[0] for row in result.fetchall()}
        print(f"Found {len(existing_script_ids)} existing tasks")

        # Get admin user id (user_id=1 for admin)
        admin_user_id = 1

        created = 0
        batch = []
        for script in scripts:
            if script.script_id in existing_script_ids:
                continue

            # Map script frequency to schedule expression
            freq = script.frequency.value if script.frequency else "daily"
            if freq == "hourly":
                schedule_type = ScheduleType.CRON
                schedule_expr = "0 * * * *"  # every hour at :00
            elif freq == "daily":
                schedule_type = ScheduleType.DAILY
                schedule_expr = "02:00"  # 2 AM daily
            elif freq == "weekly":
                schedule_type = ScheduleType.WEEKLY
                schedule_expr = "0 2 * * 1"  # Monday 2 AM
            elif freq == "monthly":
                schedule_type = ScheduleType.MONTHLY
                schedule_expr = "0 2 1 * *"  # 1st of month 2 AM
            else:
                schedule_type = ScheduleType.DAILY
                schedule_expr = "02:00"

            task = ScheduledTask(
                name=f"Auto: {script.script_name}",
                description=f"自动创建的定时任务 - {script.script_id}",
                user_id=admin_user_id,
                script_id=script.script_id,
                schedule_type=schedule_type,
                schedule_expression=schedule_expr,
                parameters={},
                is_active=True,
                retry_on_failure=True,
                max_retries=3,
                timeout=script.timeout or 300,
            )
            batch.append(task)
            created += 1

            # Commit in batches of 100
            if len(batch) >= 100:
                db.add_all(batch)
                await db.commit()
                batch = []
                print(f"  Created {created} tasks so far...")

        # Commit remaining
        if batch:
            db.add_all(batch)
            await db.commit()

        print(
            f"Created {created} new scheduled tasks (total: {created + len(existing_script_ids)})"
        )


async def sync_data_warehouse_tables():
    """Sync data warehouse tables into data_tables metadata."""
    # Connect to data warehouse using sync engine for SHOW TABLES
    from urllib.parse import quote_plus

    pw = quote_plus(settings.data_mysql_password)
    data_url = f"mysql+pymysql://{settings.data_mysql_user}:{pw}@{settings.data_mysql_host}:{settings.data_mysql_port}/{settings.data_mysql_database}"
    sync_engine = create_engine(data_url)

    # Get all tables from data warehouse
    with sync_engine.connect() as conn:
        result = conn.execute(text("SHOW TABLES"))
        warehouse_tables = {row[0] for row in result.fetchall()}
    sync_engine.dispose()

    print(
        f"Found {len(warehouse_tables)} tables in data warehouse ({settings.data_mysql_database})"
    )

    async with async_session_maker() as db:
        # Get existing data_tables entries
        result = await db.execute(select(DataTable.table_name))
        existing_tables = {row[0] for row in result.fetchall()}
        print(f"Found {len(existing_tables)} existing entries in data_tables metadata")

        # Find new tables to add
        new_tables = warehouse_tables - existing_tables
        print(f"Found {len(new_tables)} new tables to sync")

        # Try to match tables to scripts
        result = await db.execute(select(DataScript))
        scripts = result.scalars().all()
        script_map = {}
        for s in scripts:
            # Map by target_table
            if s.target_table:
                script_map[s.target_table.upper()] = s
            # Map by script_id (table names are often uppercase versions)
            script_map[s.script_id.upper()] = s

        created = 0
        batch = []
        for table_name in new_tables:
            # Try to find matching script
            matched_script = script_map.get(table_name.upper())
            category = matched_script.category if matched_script else None
            script_id = matched_script.script_id if matched_script else None

            dt = DataTable(
                table_name=table_name,
                table_comment=None,
                category=category,
                script_id=script_id,
                row_count=0,
            )
            batch.append(dt)
            created += 1

            if len(batch) >= 100:
                db.add_all(batch)
                await db.commit()
                batch = []
                print(f"  Synced {created} tables so far...")

        if batch:
            db.add_all(batch)
            await db.commit()

        print(f"Synced {created} new tables to data_tables metadata")


async def refresh_row_counts():
    """Refresh row_count for all data_tables from the actual data warehouse."""
    import re
    from urllib.parse import quote_plus

    pw = quote_plus(settings.data_mysql_password)
    data_url = f"mysql+pymysql://{settings.data_mysql_user}:{pw}@{settings.data_mysql_host}:{settings.data_mysql_port}/{settings.data_mysql_database}"
    sync_engine = create_engine(data_url)

    async with async_session_maker() as db:
        result = await db.execute(select(DataTable))
        tables = result.scalars().all()
        print(f"Refreshing row counts for {len(tables)} tables...")

        updated = 0
        errors = 0

        for table in tables:
            # Validate table name
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table.table_name):
                errors += 1
                continue

            try:
                with sync_engine.connect() as conn:
                    r = conn.execute(text(f"SELECT COUNT(*) FROM `{table.table_name}`"))
                    count = r.scalar() or 0
                    if count != table.row_count:
                        table.row_count = count
                        updated += 1
            except Exception as e:
                # Table might not exist in warehouse
                errors += 1

            if (updated + errors) % 100 == 0:
                print(
                    f"  Processed {updated + errors}/{len(tables)}... ({updated} updated, {errors} errors)"
                )

        await db.commit()
        sync_engine.dispose()
        print(f"Updated {updated} row counts ({errors} errors/missing tables)")


async def main():
    print("=" * 60)
    print("opendata: Initialize Tasks & Sync Data Tables")
    print("=" * 60)

    print("\n--- Step 1: Create scheduled tasks from scripts ---")
    await create_tasks_from_scripts()

    print("\n--- Step 2: Sync data warehouse tables to metadata ---")
    await sync_data_warehouse_tables()

    print("\n--- Step 3: Refresh row counts from data warehouse ---")
    await refresh_row_counts()

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
