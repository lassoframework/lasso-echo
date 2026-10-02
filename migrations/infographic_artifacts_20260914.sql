-- Shared provenance survives intake-web and worker restarts.
create table if not exists public.echo_infographic_artifacts (
    tenant text not null,
    image_url text not null,
    image_sha256 text not null,
    evidence jsonb not null,
    source_identity jsonb not null,
    cache_key text,
    created_at timestamptz not null default now(),
    primary key (tenant, image_url),
    check (length(image_sha256) = 64)
);
create index if not exists echo_infographic_artifacts_cache
    on public.echo_infographic_artifacts(tenant, cache_key);
alter table public.echo_infographic_artifacts enable row level security;
revoke all on public.echo_infographic_artifacts from anon, authenticated;
grant select, insert, update on public.echo_infographic_artifacts to service_role;
