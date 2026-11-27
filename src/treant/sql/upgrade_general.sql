-- In-place upgrade of the general schema. Idempotent; run as the admin role.
-- Placeholders are filled with psycopg.sql.Identifier, so literal braces are doubled.

-- A fact or node that stops being current loses its vector (same as general.sql). Runs as
-- the caller (no security definer): the publisher deletes here with its own grant.
create or replace function general.drop_vector_when_not_current() returns trigger
    language plpgsql
    set search_path = pg_catalog, pg_temp
as $$
begin
    delete from general.embeddings where item_kind = tg_argv[0] and item_id = new.id;
    return new;
end
$$;
revoke execute on function general.drop_vector_when_not_current() from public;
create or replace trigger facts_drop_vector
    after update of status on general.facts
    for each row when (old.status = 'current' and new.status <> 'current')
    execute function general.drop_vector_when_not_current('fact');
create or replace trigger nodes_drop_vector
    after update of status on general.nodes
    for each row when (old.status = 'current' and new.status <> 'current')
    execute function general.drop_vector_when_not_current('node');

-- Vectors left behind by older releases: facts and nodes no longer current (or gone).
delete from general.embeddings e where e.item_kind = 'fact' and not exists (
    select 1 from general.facts f where f.id = e.item_id and f.status = 'current');
delete from general.embeddings e where e.item_kind = 'node' and not exists (
    select 1 from general.nodes n where n.id = e.item_id and n.status = 'current');

-- One vector index per item kind in place of the single embeddings_vec_idx.
drop index if exists general.embeddings_vec_idx;
create index if not exists embeddings_fact_vec_idx on general.embeddings
    using hnsw (vec vector_cosine_ops) where item_kind = 'fact';
create index if not exists embeddings_node_vec_idx on general.embeddings
    using hnsw (vec vector_cosine_ops) where item_kind = 'node';
