-- General schema: concepts shared by every workspace.
-- Run as {admin} (the owner). Placeholders are filled with psycopg.sql.Identifier,
-- so literal braces in this file are doubled.

create table if not exists general.nodes (
    id uuid primary key default gen_random_uuid(),
    kind text not null default 'concept' check (kind = 'concept'),
    type text not null,
    name text not null,
    aliases text[] not null default '{{}}',
    confidence real not null default 1 check (confidence between 0 and 1),
    valid_from timestamptz not null default now(),
    valid_to timestamptz,
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted')),
    created_at timestamptz not null default now()
);
create index if not exists nodes_name_idx on general.nodes (lower(name));
create index if not exists nodes_aliases_idx on general.nodes using gin (aliases);

create table if not exists general.edges (
    id uuid primary key default gen_random_uuid(),
    src uuid not null references general.nodes (id),
    dst uuid not null references general.nodes (id),
    type text not null check (type in ('uses', 'depends_on', 'deploys_to', 'runs_on',
                                       'part_of', 'replaced_by', 'decided_in')),
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted')),
    created_at timestamptz not null default now()
);
create index if not exists edges_src_idx on general.edges (src);
create index if not exists edges_dst_idx on general.edges (dst);

create table if not exists general.facts (
    id uuid primary key default gen_random_uuid(),
    type text not null check (type in ('fact', 'decision', 'lesson', 'dead_end', 'procedure',
                                       'change', 'open_thread', 'preference', 'warning')),
    text text not null,
    node_id uuid references general.nodes (id),
    status text not null default 'current'
        check (status in ('current', 'superseded', 'retracted', 'closed')),
    version int not null default 1,
    created_at timestamptz not null default now(),
    check (status <> 'closed' or type = 'open_thread')
);
create index if not exists facts_node_idx on general.facts (node_id);

create table if not exists general.versions (
    item_kind text not null,
    item_id uuid not null,
    version int not null,
    data jsonb not null,
    changed_at timestamptz not null default now(),
    reason text,
    primary key (item_kind, item_id, version)
);

create table if not exists general.embeddings (
    item_kind text not null,
    item_id uuid not null,
    vec vector(768) not null,
    primary key (item_kind, item_id)
);
create index if not exists embeddings_vec_idx on general.embeddings
    using hnsw (vec vector_cosine_ops);

-- Grants. Re-applied on every bootstrap. Workspace roles get SELECT here in
-- create_workspace.
revoke all on schema general from public;
revoke all on all tables in schema general from public;
grant usage on schema general to {publisher};
grant select, insert, update, delete on all tables in schema general to {publisher};
grant usage, select on all sequences in schema general to {publisher};
