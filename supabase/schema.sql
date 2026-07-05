-- kratosNet Supabase schema
-- Τρέξε αυτό ΜΙΑ φορά στο Supabase Dashboard → SQL Editor → New query → Run
--
-- Σχεδιασμένο για το FREE tier:
--   * 1 πίνακας προφίλ (συγχρονισμός προτιμήσεων μεταξύ συσκευών)
--   * 1 πίνακας ιστορικού ειδοποιήσεων (για να μη στέλνουμε διπλά email)
--   * Row Level Security: κάθε χρήστης βλέπει/αλλάζει ΜΟΝΟ τα δικά του δεδομένα

-- ============ ΠΡΟΦΙΛ ΧΡΗΣΤΗ ============
create table if not exists public.profiles (
  id uuid references auth.users(id) on delete cascade primary key,
  email text,
  kad text default '',
  region text default '',
  muni_name text default 'Δήμος Αγρινίου',
  muni_uid text default '6012',
  -- Ειδοποιήσεις: απενεργοποιημένες by default — ο χρήστης κάνει opt-in ρητά
  notify_email boolean default false,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

alter table public.profiles enable row level security;

-- Ο καθένας διαβάζει/γράφει μόνο το δικό του προφίλ
create policy "read own profile"
  on public.profiles for select
  using (auth.uid() = id);

create policy "insert own profile"
  on public.profiles for insert
  with check (auth.uid() = id);

create policy "update own profile"
  on public.profiles for update
  using (auth.uid() = id);

-- Αυτόματη δημιουργία προφίλ στο πρώτο login
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ============ ΙΣΤΟΡΙΚΟ ΕΙΔΟΠΟΙΗΣΕΩΝ ============
-- Κρατάει ποιο (χρήστης, πρόγραμμα) ζεύγος έχει ήδη ειδοποιηθεί,
-- ώστε το nightly script να μη στέλνει το ίδιο email δύο φορές.
create table if not exists public.notified (
  user_id uuid references auth.users(id) on delete cascade,
  program_id text not null,
  sent_at timestamptz default now(),
  primary key (user_id, program_id)
);

alter table public.notified enable row level security;
-- Κανένα public policy: μόνο το service-role key (nightly script) γράφει εδώ.
-- Ο χρήστης δεν χρειάζεται πρόσβαση σε αυτόν τον πίνακα από το frontend.
