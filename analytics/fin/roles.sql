-- Роль Metabase для форм финмодели. Пароль — из переменной окружения, как
-- у metabase_ro (analytics/metabase_roles.sql):
--
--   docker exec -i -e MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" \
--     teashop-postgres psql -U teashop -d teashop -v ON_ERROR_STOP=1 \
--     -f - < analytics/fin/roles.sql
--
-- После — права: analytics/fin/grants.sql (apply.sh выполняет его сам).

\set ON_ERROR_STOP on
\getenv fin_pw MB_FIN_PW

select format('create role metabase_fin login password %L', :'fin_pw')
 where not exists (select from pg_roles where rolname = 'metabase_fin') \gexec
select format('alter role metabase_fin login password %L', :'fin_pw') \gexec
-- Формы пишут, поэтому не read only, как metabase_ro; остальное — так же.
alter role metabase_fin set statement_timeout = '30s';
alter role metabase_fin connection limit 5;
revoke all on schema public from metabase_fin;
revoke create on schema public from metabase_fin;
