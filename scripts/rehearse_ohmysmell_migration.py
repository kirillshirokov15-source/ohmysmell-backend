"""Rehearse migrations only on a newly restored, verified loopback database."""
import argparse
import hashlib
import json
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from alembic import command
from alembic.config import Config
from scripts.restore_local_backup import restore


def snapshot(connection, columns):
    connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    quote=connection.dialect.identifier_preparer.quote
    result={}
    for table,names in columns.items():
        if table=='alembic_version':
            continue
        projection=','.join(map(quote,names))
        rows=connection.execute(text(f'SELECT row_to_json(t)::text FROM (SELECT {projection} FROM public.{quote(table)}) t')).scalars().all()
        result[table]={'rows':len(rows),'sha256':hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup_directory')
    args=parser.parse_args()
    restored=restore(args.backup_directory,'.staging-artifacts/acceptance-pg-20261010')
    name=restored['database']
    engine=create_engine('postgresql://oms_test@127.0.0.1:55439/'+name)
    try:
        with engine.begin() as c:
            inspector=inspect(c)
            columns={t:[col['name'] for col in inspector.get_columns(t)] for t in inspector.get_table_names()}
            before=snapshot(c,columns)
            config=Config('alembic.ini'); config.attributes['connection']=c
            command.upgrade(config,'r71fd865eb98')
            command.check(config)
            if snapshot(c,columns)!=before:
                raise ValueError('Existing data changed during upgrade')
            command.downgrade(config,'p59db643bc76')
            if snapshot(c,columns)!=before:
                raise ValueError('Existing data changed during unused-feature downgrade')
            command.upgrade(config,'r71fd865eb98')
            command.check(config)
        report={'database':name,'upgrade':'passed','original_columns_unchanged':True,
                'unused_feature_downgrade':'passed','reupgrade_check':'passed'}
        Path('.staging-artifacts/ohmysmell-1-migration.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
