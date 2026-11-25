-- One workspace schema. Run as {admin} (the owner) inside create_workspace's transaction.
-- {schema}, {role}, {gate}, {admin} and {policy} are psycopg.sql.Identifier values,
-- {workspace} is a psycopg.sql.Literal; literal braces in this file are doubled.

create table {schema}.sessions (
    id uuid primary key default gen_random_uuid(),
    token_id text not null,
    client_session_id text not null,
    created_at timestamptz not null default now(),
    unique (token_id, client_session_id)
);

create table {schema}.revisions (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references {schema}.sessions (id),
    n int not null,
    sections jsonb not null,
    status text not null default 'stored' check (status in ('stored', 'quarantined')),
    created_at timestamptz not null default now(),
    unique (session_id, n)
);

create table {schema}.journal_entries (
    id uuid primary key default gen_random_uuid(),
    revision_id uuid not null unique references {schema}.revisions (id),
    text text not null,
    created_at timestamptz not null default now()
);

create table {schema}.nodes (
    id uuid primary key default gen_random_uuid(),
    kind text not null check (kind in ('local_concept', 'instance')),
    type text not null,
    name text not null,
    aliases text[] not null default '{{}}',
    refines uuid references general.nodes (id),
    confidence real not null default 1 check (confidence between 0 and 1),
    valid_from timestamptz not null default now(),
    valid_to timestamptz,
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted')),
    created_at timestamptz not null default now(),
    check (kind <> 'local_concept' or refines is not null)
);
create index on {schema}.nodes (lower(name));
create index on {schema}.nodes using gin (aliases);
create index on {schema}.nodes (refines);

create table {schema}.edges (
    id uuid primary key default gen_random_uuid(),
    src uuid not null references {schema}.nodes (id),
    dst uuid not null references {schema}.nodes (id),
    type text not null check (type in ('uses', 'depends_on', 'deploys_to', 'runs_on',
                                       'part_of', 'replaced_by', 'decided_in')),
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted')),
    created_at timestamptz not null default now()
);
create index on {schema}.edges (src);
create index on {schema}.edges (dst);

create table {schema}.facts (
    id uuid primary key default gen_random_uuid(),
    type text not null check (type in ('fact', 'decision', 'lesson', 'dead_end', 'procedure',
                                       'change', 'open_thread', 'preference', 'warning')),
    text text not null,
    node_id uuid references {schema}.nodes (id),
    revision_id uuid references {schema}.revisions (id),
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted', 'closed')),
    version int not null default 1,
    created_at timestamptz not null default now(),
    check (status <> 'closed' or type = 'open_thread')
);
create index on {schema}.facts (node_id);
create index on {schema}.facts (revision_id);

create table {schema}.support (
    item_kind text not null,
    item_id uuid not null,
    revision_id uuid not null references {schema}.revisions (id),
    primary key (item_kind, item_id, revision_id)
);
create index on {schema}.support (revision_id);

create table {schema}.versions (
    item_kind text not null,
    item_id uuid not null,
    version int not null,
    data jsonb not null,
    changed_at timestamptz not null default now(),
    reason text,
    primary key (item_kind, item_id, version)
);

create table {schema}.embeddings (
    item_kind text not null,
    item_id uuid not null,
    vec vector(768) not null,
    primary key (item_kind, item_id)
);
create index on {schema}.embeddings using hnsw (vec vector_cosine_ops);

create table {schema}.targets (
    kind text not null check (kind in ('kube_context', 'aws_profile', 'git_host', 'tf_root',
                                       'ansible_inventory')),
    value text not null,
    primary key (kind, value)
);

-- Runs with the owner's rights, so the gate can read it without touching nodes.
create view {schema}.instance_names as
    select name, aliases from {schema}.nodes where kind = 'instance';

-- Grants: the workspace role has DML on its own schema and read-only general.
revoke all on schema {schema} from public;
grant usage on schema {schema} to {role};
grant select, insert, update, delete on all tables in schema {schema} to {role};
revoke insert, update, delete on {schema}.instance_names from {role};
grant usage, select on all sequences in schema {schema} to {role};
grant usage on schema general to {role};
grant select on all tables in schema general to {role};

-- Control: insert-only into jobs and mistakes, and only rows for this workspace. INSERT is
-- granted per column (in upgrade_workspace.sql, which create_workspace runs next), so a
-- workspace cannot plant a running or dead job or pick an id.
grant usage on schema control to {role};
grant usage on sequence control.jobs_id_seq, control.mistakes_id_seq to {role};
create policy {policy} on control.jobs for insert to {role}
    with check (workspace = {workspace});
create policy {policy} on control.mistakes for insert to {role}
    with check (workspace = {workspace});

-- Gate: the instance names view only.
grant usage on schema {schema} to {gate};
grant select on {schema}.instance_names to {gate};
