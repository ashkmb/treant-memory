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

-- A client's note that a fact names things a repository no longer has. Only the owner or an
-- agent through update_fact/retract changes the fact; a kept flag with the same text_sha256
-- suppresses new flags until the text changes.
create table if not exists {schema}.stale_flags (
    id bigserial primary key,
    fact_id uuid not null references {schema}.facts (id),
    repo text not null,
    commit text not null,
    missing jsonb not null,
    fact_version int not null,
    text_sha256 text not null,
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolution text check (resolution in ('kept', 'retracted', 'superseded-by-edit')),
    unique (fact_id, commit)
);
create unique index if not exists stale_flags_one_open
    on {schema}.stale_flags (fact_id) where resolved_at is null;

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
