-- In-place upgrade of the control schema. Idempotent; runs as the admin role after control.sql.
-- Literal braces are doubled.

alter table control.jobs add column if not exists lease_id uuid;
alter table control.jobs drop constraint if exists jobs_status_check;
alter table control.jobs add constraint jobs_status_check
    check (status in ('queued', 'running', 'done', 'dead', 'skipped'));

create table if not exists control.general_queue (
    id bigserial primary key,
    workspace text not null references control.workspaces (name),
    session_id uuid not null,
    revision_id uuid not null,
    item jsonb not null,
    status text not null default 'pending'
        check (status in ('pending', 'waiting_review', 'blocked', 'published', 'dropped',
                          'stale')),
    reasons jsonb not null default '[]',
    rewrites int not null default 0,
    published_id uuid,
    created_at timestamptz not null default now(),
    decided_at timestamptz
);
create index if not exists general_queue_ws_status_idx
    on control.general_queue (workspace, status);

-- Idempotency key of a queued item within its revision: kind + ':' + sha256 of the
-- normalised (lower-cased, whitespace-collapsed) name or text. Hashed so long fact texts stay
-- inside the btree row limit.
alter table control.general_queue add column if not exists item_key text;
create unique index if not exists general_queue_item_key_idx
    on control.general_queue (workspace, revision_id, item_key);

-- Newest revision n per session whose general half was handled; the publisher step of an older
-- revision that lost the race returns without writing.
create table if not exists control.general_session_head (
    workspace text not null,
    session_id uuid not null,
    n int not null,
    primary key (workspace, session_id)
);

-- No FK on workspace: purge keeps these rows and the origin then reads "removed workspace".
create table if not exists control.general_origin (
    item_kind text not null,
    item_id uuid not null,
    workspace text not null,
    session_id uuid not null,
    revision_id uuid not null,
    queue_id bigint,
    at timestamptz not null default now(),
    primary key (item_kind, item_id, revision_id)
);

grant select, insert, update, delete on all tables in schema control to {control};
grant usage, select on all sequences in schema control to {control};
grant usage on schema control to {publisher};
grant select, insert, update on control.general_queue, control.general_origin to {publisher};
grant usage, select on sequence control.general_queue_id_seq to {publisher};
grant select, insert, update on control.general_session_head to {publisher};

-- The publisher enqueues gate and apply jobs for any workspace; insert only, it cannot read jobs.
revoke insert on control.jobs from {publisher};
grant insert (workspace, kind, key, payload, run_after) on control.jobs to {publisher};
grant usage on sequence control.jobs_id_seq to {publisher};
drop policy if exists publisher_insert on control.jobs;
create policy publisher_insert on control.jobs for insert to {publisher}
    with check (kind in ('gate', 'apply'));

-- Audit stays append-only for everyone, the owner included.
revoke update, delete, truncate on control.audit from {control};
revoke update, delete, truncate on control.audit from {admin};

-- Functions the admin role creates are not callable by every role by default.
alter default privileges for role {admin} revoke execute on functions from public;
revoke execute on all functions in schema control from public;
revoke execute on all functions in schema general from public;
