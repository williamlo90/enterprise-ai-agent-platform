"""Explicit operator-only fictional trip seeding into owned disposable schemas."""
import re
from psycopg import sql
from reimbursement_service.modules.trips.domain import Trip,identifier


async def seed_trips(connection,schema,tenant_id,owner_id,trips):
    if not re.fullmatch(r'ref01_probe_[a-f0-9]{32}',schema):raise ValueError('Only disposable fixture schemas may be seeded.')
    identifier(tenant_id);identifier(owner_id)
    if type(trips) is not tuple or any(type(trip) is not Trip for trip in trips):raise ValueError('Invalid fictional trip fixture.')
    if len({trip.id for trip in trips})!=len(trips):raise ValueError('Duplicate trip identity.')
    async with connection.transaction():
        for trip in trips:
            row=await (await connection.execute(sql.SQL('SELECT owner_principal_ref,start_date,end_date,state,employee_ref FROM {}.trips WHERE tenant_id=%s AND id=%s FOR UPDATE').format(sql.Identifier(schema)),(tenant_id,trip.id))).fetchone()
            if row is not None:
                if (str(row[0]),row[1].isoformat(),row[2].isoformat(),row[3],row[4])!=(owner_id,trip.start_date,trip.end_date,trip.state,trip.employee_ref):raise ValueError('Fixture identity conflict.')
                children=await (await connection.execute(sql.SQL('SELECT id,expense_date,category,declared_amount_minor,currency FROM {}.trip_expense_items WHERE tenant_id=%s AND trip_id=%s ORDER BY id').format(sql.Identifier(schema)),(tenant_id,trip.id))).fetchall()
                normalized=[dict(id=str(r[0]),expense_date=r[1].isoformat(),category=r[2],declared_amount_minor=r[3],currency=r[4]) for r in children]
                if normalized!=trip.wire()['items']:raise ValueError('Fixture item conflict.')
                continue
            await connection.execute(sql.SQL('INSERT INTO {}.trips VALUES(%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(schema)),
                (tenant_id,trip.id,owner_id,trip.start_date,trip.end_date,trip.employee_ref,trip.state))
            for item in trip.items:
                await connection.execute(sql.SQL('INSERT INTO {}.trip_expense_items VALUES(%s,%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(schema)),
                    (tenant_id,item.id,trip.id,owner_id,item.expense_date,item.category,item.declared_amount_minor,item.currency))
    return len(trips)
