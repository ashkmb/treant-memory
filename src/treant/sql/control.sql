-- Control schema: workspaces, tokens, job queue, audit, mistakes, canaries, metrics.
-- Run as {admin} (the owner). Placeholders are filled with psycopg.sql.Identifier,
-- so literal braces in this file are doubled.

create table if not exists control.workspaces (
    name text primary key,
    status text not null default 'active' check (status in ('active', 'archived')),
    settings jsonb not null default '{{}}',
    created_at timestamptz not null default now()
);

create table if not exists control.tokens (
    id text primary key,
    workspace text not null references control.workspaces (name),
    label text not null default '',
    secret_sha256 text not null,
    actions text[] not null
        check (cardinality(actions) > 0
               and actions <@ array['create', 'read', 'update', 'delete']::text[]),
    created_at timestamptz not null default now(),
    revoked_at timestamptz,
    last_used_at timestamptz
);
create index if not exists tokens_workspace_idx on control.tokens (workspace);

create table if not exists control.jobs (
    id bigserial primary key,
    workspace text not null references control.workspaces (name),
    kind text not null,
    key text not null,
    payload jsonb not null default '{{}}',
    status text not null default 'queued'
        check (status in ('queued', 'running', 'done', 'dead')),
    attempts int not null default 0,
    run_after timestamptz not null default now(),
    lease_until timestamptz,
    error_category text,
    created_at timestamptz not null default now(),
    unique (workspace, kind, key)
);
create index if not exists jobs_ready_idx on control.jobs (run_after) where status = 'queued';
create index if not exists jobs_lease_idx on control.jobs (lease_until) where status = 'running';

create table if not exists control.audit (
    id bigserial primary key,
    at timestamptz not null default now(),
    actor text not null,
    action text not null,
    detail jsonb not null default '{{}}'
);
create index if not exists audit_at_idx on control.audit (at);

create table if not exists control.mistakes (
    id bigserial primary key,
    workspace text not null references control.workspaces (name),
    at timestamptz not null default now(),
    text text not null
);
create index if not exists mistakes_workspace_idx on control.mistakes (workspace, at);

create table if not exists control.canaries (
    workspace text not null references control.workspaces (name),
    marker text not null,
    item_kind text not null,
    item_id uuid not null,
    primary key (workspace, item_kind, item_id)
);
create index if not exists canaries_marker_idx on control.canaries (marker);

create table if not exists control.metrics (
    name text not null,
    workspace text references control.workspaces (name),
    at timestamptz not null default now(),
    value double precision not null
);
create index if not exists metrics_name_at_idx on control.metrics (name, at);

-- Grants. Re-applied on every bootstrap.
revoke all on schema control from public;
revoke all on all tables in schema control from public;
grant usage on schema control to {control};
grant select, insert, update, delete on all tables in schema control to {control};
grant usage, select on all sequences in schema control to {control};

-- Audit is append-only for everyone, the owner included.
revoke update, delete, truncate on control.audit from {control};
revoke update, delete, truncate on control.audit from {admin};

-- Workspace roles insert into jobs and mistakes under per-workspace policies
-- (added by create_workspace); the control role sees every row.
alter table control.jobs enable row level security;
alter table control.mistakes enable row level security;
drop policy if exists control_all on control.jobs;
create policy control_all on control.jobs to {control} using (true) with check (true);
drop policy if exists control_all on control.mistakes;
create policy control_all on control.mistakes to {control} using (true) with check (true);
