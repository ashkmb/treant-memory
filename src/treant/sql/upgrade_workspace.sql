-- In-place upgrade of one workspace schema. Idempotent; run as the admin role.
-- {schema} and {role} are psycopg.sql.Identifier values; literal braces are doubled.

alter table {schema}.revisions drop constraint if exists revisions_status_check;
alter table {schema}.revisions add constraint revisions_status_check
    check (status in ('stored', 'quarantined', 'dropped'));

create table if not exists {schema}.generations (
    revision_id uuid primary key references {schema}.revisions (id),
    output jsonb not null,
    created_at timestamptz not null default now()
);

create table if not exists {schema}.merge_flags (
    id bigserial primary key,
    node_a uuid,
    node_b uuid,
    score real,
    created_at timestamptz not null default now(),
    unique (node_a, node_b)
);

create table if not exists {schema}.conflicts (
    id bigserial primary key,
    new_fact uuid,
    old_fact uuid,
    created_at timestamptz not null default now(),
    unique (new_fact, old_fact)
);

-- Owner resolution of a merge flag or a conflict; null while open.
alter table {schema}.merge_flags add column if not exists resolved_at timestamptz;
alter table {schema}.merge_flags add column if not exists resolution text;
alter table {schema}.conflicts add column if not exists resolved_at timestamptz;
alter table {schema}.conflicts add column if not exists resolution text;

-- Concept name a fact waits for; null otherwise.
alter table {schema}.facts add column if not exists wait_concept text;

-- Number of spans the scrubber removed from the save.
alter table {schema}.revisions add column if not exists removed int not null default 0;

-- A fact or node that stops being current loses its vector (same as workspace.sql).
create or replace function {schema}.drop_vector_when_not_current() returns trigger
    language plpgsql
    set search_path = pg_catalog, pg_temp
as $$
begin
    delete from {schema}.embeddings where item_kind = tg_argv[0] and item_id = new.id;
    return new;
end
$$;
revoke execute on function {schema}.drop_vector_when_not_current() from public;
create or replace trigger facts_drop_vector
    after update of status on {schema}.facts
    for each row when (old.status = 'current' and new.status <> 'current')
    execute function {schema}.drop_vector_when_not_current('fact');
create or replace trigger nodes_drop_vector
    after update of status on {schema}.nodes
    for each row when (old.status = 'current' and new.status <> 'current')
    execute function {schema}.drop_vector_when_not_current('node');

-- Vectors left behind by older releases: facts and nodes no longer current (or gone), and
-- journal entries whose revision is no longer stored (or that no longer exist).
delete from {schema}.embeddings e where e.item_kind = 'fact' and not exists (
    select 1 from {schema}.facts f where f.id = e.item_id and f.status = 'current');
delete from {schema}.embeddings e where e.item_kind = 'node' and not exists (
    select 1 from {schema}.nodes n where n.id = e.item_id and n.status = 'current');
delete from {schema}.embeddings e where e.item_kind = 'journal' and not exists (
    select 1 from {schema}.journal_entries j join {schema}.revisions r on r.id = j.revision_id
    where j.id = e.item_id and r.status = 'stored');

-- One vector index per item kind in place of the single unnamed one (embeddings_vec_idx).
drop index if exists {schema}.embeddings_vec_idx;
create index if not exists embeddings_fact_vec_idx on {schema}.embeddings
    using hnsw (vec vector_cosine_ops) where item_kind = 'fact';
create index if not exists embeddings_node_vec_idx on {schema}.embeddings
    using hnsw (vec vector_cosine_ops) where item_kind = 'node';
create index if not exists embeddings_journal_vec_idx on {schema}.embeddings
    using hnsw (vec vector_cosine_ops) where item_kind = 'journal';

grant select, insert, update, delete on all tables in schema {schema} to {role};
revoke insert, update, delete on {schema}.instance_names from {role};
grant usage, select on all sequences in schema {schema} to {role};

-- Queue inserts name only the columns enqueue and report_mistake write; the rest keep their
-- defaults. The revoke also clears the whole-table INSERT older releases granted.
revoke insert on control.jobs, control.mistakes from {role};
grant insert (workspace, kind, key, payload, run_after) on control.jobs to {role};
grant insert (workspace, text) on control.mistakes to {role};

revoke execute on all functions in schema {schema} from public;

-- Read-only general, re-granted so general tables added by later releases are readable.
grant usage on schema general to {role};
grant select on all tables in schema general to {role};
