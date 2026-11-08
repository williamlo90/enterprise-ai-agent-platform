-- Only after old reader/writer retirement and successful compatibility/backfill checks.
LOCK TABLE {schema}.{entry} IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS(SELECT 1 FROM {schema}.{entry} WHERE display_label IS NULL OR display_label IS DISTINCT FROM label) THEN
        RAISE EXCEPTION 'Backfill is incomplete.' USING ERRCODE='23514';
    END IF;
END;
$$;
ALTER TABLE {schema}.{entry} ALTER COLUMN display_label SET NOT NULL;
DROP TRIGGER {sync_trigger} ON {schema}.{entry};
DROP FUNCTION {schema}.{sync_function}();
DROP INDEX {schema}.{backfill_index};
ALTER TABLE {schema}.{entry} DROP COLUMN label;
