-- 020: без JIT для ролей Metabase — карточки «Пульса» за доли секунды, а не за 10–40 с.
--
-- Замер 24.10.2026: «Требует внимания» выполнялась 17,6 с, из них 3,3 с на каждое
-- представление цепочки уходило на JIT-компиляцию (628 функций, Optimization + Emission),
-- а само выполнение — миллисекунды. Оценки стоимости у представлений fin выше порога
-- jit_above_cost, и Postgres компилировал каждый запрос заново. На ВМ одно ядро: девять
-- карточек вкладки компилировались одновременно — вкладка «Сегодня» грузилась 30 с, часть
-- карточек Metabase не дожидался («Возникла проблема с отображением»).
--
-- С jit = off та же «Требует внимания» — 0,2 с, вся вкладка «Сегодня» — 0,8 с, «Отчёты» —
-- 0,4 с. Данных мало, JIT здесь только вредит. Только роли Metabase: запросы бота короткие
-- и JIT не включают. Настройка роли действует на новых подключениях: после применения
-- Metabase переподключается сам (пул обновляется), быстрее — перезапуск контейнера.

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'metabase_ro') then
        alter role metabase_ro set jit = off;
    end if;
    if exists (select 1 from pg_roles where rolname = 'metabase_fin') then
        alter role metabase_fin set jit = off;
    end if;
end $$;

insert into fin.schema_migrations (version) values ('020_metabase_no_jit') on conflict do nothing;
