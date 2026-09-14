create table if not exists public.echo_infographic_jobs (
    tenant text not null,
    cache_key text not null,
    owner uuid not null,
    lease_until timestamptz not null,
    primary key (tenant, cache_key)
);
alter table public.echo_infographic_jobs enable row level security;
revoke all on public.echo_infographic_jobs from anon, authenticated;
grant select, insert, update on public.echo_infographic_jobs to service_role;

create or replace function public.claim_infographic_job(p_tenant text, p_key text, p_owner uuid)
returns boolean language plpgsql set search_path = public as $$
declare claimed integer;
begin
    insert into public.echo_infographic_jobs(tenant, cache_key, owner, lease_until)
    values(p_tenant, p_key, p_owner, now() + interval '45 minutes')
    on conflict (tenant, cache_key) do update
    set owner=excluded.owner, lease_until=excluded.lease_until
    where echo_infographic_jobs.lease_until < now();
    get diagnostics claimed = row_count;
    return claimed = 1;
end;
$$;
revoke all on function public.claim_infographic_job(text,text,uuid) from public, anon, authenticated;
grant execute on function public.claim_infographic_job(text,text,uuid) to service_role;
