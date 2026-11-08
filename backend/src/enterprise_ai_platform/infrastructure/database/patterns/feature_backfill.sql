-- One resumable bounded batch. Zero updates can mean rows are locked, not completion.
WITH batch AS (
    SELECT tenant_id,id FROM {schema}.{entry} WHERE display_label IS NULL
    ORDER BY tenant_id,id LIMIT {batch_size} FOR UPDATE SKIP LOCKED
), changed AS (
    UPDATE {schema}.{entry} e SET display_label=e.label,version=e.version+1,updated_at=clock_timestamp()
    FROM batch b WHERE e.tenant_id=b.tenant_id AND e.id=b.id RETURNING e.id
) SELECT count(*)::integer FROM changed;
