-- 엔진 대시보드 (클라우드 · 선택 기능) — 내 Supabase 프로젝트의 SQL Editor 에서 한 번 실행
-- 읽기 = 로그인한 관리자만 · 쓰기 = PC 업로더(secret key — RLS를 거치지 않음)만
-- 다시 실행해도 안전하다(있으면 건너뛰거나 같은 내용으로 덮어씀)

-- 1) 관리자 판정: 로그인 이메일이 이것일 때만 — ⚠️ 실행 전에 아래 이메일을 내 이메일로 바꾼다
create or replace function public.aw_is_admin() returns boolean
language sql stable security invoker set search_path = ''
as $$ select coalesce((auth.jwt() ->> 'email') = 'you@example.com', false) $$;
revoke all on function public.aw_is_admin() from public, anon;
grant execute on function public.aw_is_admin() to authenticated;

-- 2) run 표: 행 1개 = run 1개 (목록용 칸 + 화면 데이터 전체 payload)
create table if not exists public.aw_runs (
  id         text primary key,             -- "2026-10-03/run003_…" (PC ai-log 폴더 이름)
  created    text,
  status     text,
  stage      text,
  calls      integer,
  cost       numeric,
  campaign   text,
  feature    text,
  hb_epoch   double precision,             -- 마지막 heartbeat (살아 있음은 사이트가 계산)
  payload    jsonb not null,
  updated_at timestamptz not null default now()
);
create index if not exists aw_runs_updated_at on public.aw_runs (updated_at desc);
alter table public.aw_runs enable row level security;

revoke all on public.aw_runs from anon, authenticated;
grant select on public.aw_runs to authenticated;
grant select, insert, update, delete on public.aw_runs to service_role;

drop policy if exists "aw_runs_admin_read" on public.aw_runs;
create policy "aw_runs_admin_read" on public.aw_runs
  for select to authenticated using (public.aw_is_admin());

-- 3) 파일 보관함(raw 원문 · 캡처 · 판정 · 시나리오 로그): 비공개, 파일당 50MB
insert into storage.buckets (id, name, public, file_size_limit)
values ('aw-files', 'aw-files', false, 52428800)
on conflict (id) do nothing;

drop policy if exists "aw_files_admin_read" on storage.objects;
create policy "aw_files_admin_read" on storage.objects
  for select to authenticated using (bucket_id = 'aw-files' and public.aw_is_admin());

-- 4) 확인: 아래 결과에 정책 2줄이 나오면 끝
select schemaname, tablename, policyname
from pg_policies
where policyname in ('aw_runs_admin_read', 'aw_files_admin_read');
