-- Vista de clientes: solo el admin ve todo; los clientes aprobados solo
-- ven señales y velas. Correr una vez en Supabase > SQL Editor.

create table if not exists public.admins (
  email text primary key
);
create table if not exists public.clientes (
  email text primary key,
  nombre text,
  activo boolean not null default true,
  created_at timestamptz not null default now()
);
alter table public.admins enable row level security;
alter table public.clientes enable row level security;

insert into public.admins(email) values ('arioldys06@gmail.com') on conflict do nothing;

create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.admins a where lower(a.email) = lower(coalesce(auth.jwt()->>'email','')));
$$;

create or replace function public.is_cliente_activo() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.clientes c where c.activo and lower(c.email) = lower(coalesce(auth.jwt()->>'email','')));
$$;

revoke all on function public.is_admin() from public;
revoke all on function public.is_cliente_activo() from public;
grant execute on function public.is_admin() to authenticated;
grant execute on function public.is_cliente_activo() to authenticated;

-- admins: cada usuario solo puede ver si el mismo es admin
drop policy if exists "admins ver propio" on public.admins;
create policy "admins ver propio" on public.admins for select to authenticated
  using (lower(email) = lower(coalesce(auth.jwt()->>'email','')));

-- clientes: el admin gestiona todo; el cliente ve su propia fila
drop policy if exists "clientes admin todo" on public.clientes;
create policy "clientes admin todo" on public.clientes for all to authenticated
  using (public.is_admin()) with check (public.is_admin());
drop policy if exists "clientes ver propio" on public.clientes;
create policy "clientes ver propio" on public.clientes for select to authenticated
  using (lower(email) = lower(coalesce(auth.jwt()->>'email','')));

-- señales y velas: admin o cliente activo
drop policy if exists "Leer signals autenticado" on public.signals;
drop policy if exists "Leer signals admin o cliente" on public.signals;
create policy "Leer signals admin o cliente" on public.signals for select to authenticated
  using (public.is_admin() or public.is_cliente_activo());

drop policy if exists "Leer ohlc autenticado" on public.ohlc_candles;
drop policy if exists "Leer ohlc admin o cliente" on public.ohlc_candles;
create policy "Leer ohlc admin o cliente" on public.ohlc_candles for select to authenticated
  using (public.is_admin() or public.is_cliente_activo());

-- solo admin: analisis, backtests y trades reales (P&L)
drop policy if exists "Leer analysis autenticado" on public.analysis_results;
drop policy if exists "Leer analysis admin" on public.analysis_results;
create policy "Leer analysis admin" on public.analysis_results for select to authenticated
  using (public.is_admin());

drop policy if exists "Leer backtests autenticado" on public.backtests;
drop policy if exists "Leer backtests admin" on public.backtests;
create policy "Leer backtests admin" on public.backtests for select to authenticated
  using (public.is_admin());

-- Antes trades_ejecutados era legible por CUALQUIERA con la clave publica
-- (anon), sin login. Ahora solo el admin.
drop policy if exists "Permitir lectura publica de trades" on public.trades_ejecutados;
drop policy if exists "Leer trades admin" on public.trades_ejecutados;
create policy "Leer trades admin" on public.trades_ejecutados for select to authenticated
  using (public.is_admin());
