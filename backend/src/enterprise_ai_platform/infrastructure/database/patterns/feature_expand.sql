-- Commit this revision before running batches or deploying the new writer.
ALTER TABLE {schema}.{entry} ADD COLUMN display_label text;
ALTER TABLE {schema}.{entry} ADD CONSTRAINT {display_constraint}
    CHECK(display_label IS NULL OR (length(btrim(display_label))>0 AND octet_length(display_label)<=200));
CREATE FUNCTION {schema}.{sync_function}() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.label IS NULL THEN NEW.label:=NEW.display_label; END IF;
        IF NEW.display_label IS NULL THEN NEW.display_label:=NEW.label; END IF;
    ELSE
        IF NEW.label IS DISTINCT FROM OLD.label AND NEW.display_label IS NOT DISTINCT FROM OLD.display_label THEN
            NEW.display_label:=NEW.label;
        ELSIF NEW.display_label IS DISTINCT FROM OLD.display_label AND NEW.label IS NOT DISTINCT FROM OLD.label THEN
            NEW.label:=NEW.display_label;
        ELSIF NEW.display_label IS NULL AND NEW.label IS NOT DISTINCT FROM OLD.label THEN
            NEW.display_label:=NEW.label;
        END IF;
    END IF;
    IF NEW.label IS DISTINCT FROM NEW.display_label OR NEW.label IS NULL THEN
        RAISE EXCEPTION 'Inconsistent transition write.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION {schema}.{sync_function}() FROM PUBLIC,{access_role};
CREATE TRIGGER {sync_trigger} BEFORE INSERT OR UPDATE ON {schema}.{entry}
    FOR EACH ROW EXECUTE FUNCTION {schema}.{sync_function}();
CREATE INDEX {backfill_index} ON {schema}.{entry}(tenant_id,id) WHERE display_label IS NULL;
GRANT UPDATE(display_label) ON {schema}.{entry} TO {access_role};
