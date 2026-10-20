-- 009: исправления цифр к дашбордам «Пульс» и «Служебное» (разбор 20.10.2026, пакет D).
--
-- Что меняется:
-- - D1  среднее движения денег за 3 месяца — среднее, а не сумма (fin.v_cash_monthly);
-- - D2  покупки с личной карты: переводы себе размечаются («на покупки», «вывод
--       владельцем», «между своими счетами»), расходы бизнеса = выписка без
--       переводов себе + записи «мимо счёта», сверка «не разнесено»;
-- - D3  оборудование — во «Вложениях», категории выписки и «мимо счёта» сводятся
--       к одним статьям бюджета пилота (fin.budget_article_map);
-- - D4  «свободно сверх резерва» и «денег хватит до» — по прогнозу с планом;
-- - D5  вклады партнёров: внесено против плана;
-- - D6  бюджет пилота: план, потрачено, осталось;
-- - D7  безубыточность М1–М3 по рекламе из вводных и сценарии 10 / 30 / 60 тыс.;
-- - D9  названия товаров без кода поставщика;
-- - D10 флаг низкой маржи;
-- - D12 Claude в рублях, сверка счёта Anthropic с учётом бота;
-- - D13 ДДС по-русски; D14 P&L статьями; D15 статус пилота;
-- - «Требует внимания» — одна витрина проверок.
--
-- Правила те же, что в 001–008: таблицы бота и finance (cashflow) не меняются,
-- только читаются; существующие представления не трогаются — всё новое новыми
-- представлениями; вводные — новыми строками; «нет данных» — NULL.
-- Контрагенты и назначения платежей наружу не выходят: представления отдают
-- суммы, даты, статьи и идентификатор операции банка.

-- --- вводные -------------------------------------------------------------------------
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select * from (values
    -- D7: реклама по плану, своими деньгами (бонусы ВК — отдельно, в CAC не входят).
    ('ad_plan_m1', 'Реклама по плану: М1, свои', 10000::numeric, null::text, '₽', 'Допущение',
     'Как первый рекламный тест (first_ad_test_own)', '2026-10-20'::date, 'миграция'),
    ('ad_plan_m2', 'Реклама по плану: М2, свои', 25000, null, '₽', 'Допущение',
     'Пополнение ВК 60 000 ₽ минус тест М1, поровну на М2 и М3 — уточнить', '2026-10-20', 'миграция'),
    ('ad_plan_m3', 'Реклама по плану: М3, свои', 25000, null, '₽', 'Допущение',
     'Пополнение ВК 60 000 ₽ минус тест М1, поровну на М2 и М3 — уточнить', '2026-10-20', 'миграция'),
    ('ad_scenarios', 'Сценарии рекламы в месяц для безубыточности', null, '10000,30000,60000', '₽ через запятую',
     'Решение 20.10', 'Сколько заказов нужно при каждой сумме рекламы', '2026-10-20', 'миграция'),
    -- D10: порог низкой маржи до рекламы.
    ('low_margin_threshold', 'Порог низкой маржи до рекламы', 0.20, null, '%', 'Допущение',
     'Ниже — красным на «Пульсе → Товар»', '2026-10-20', 'миграция'),
    -- D4: когда ждём оставшиеся вклады по плану.
    ('capital_expected_date', 'Когда ждём оставшиеся вклады', null, '2026-10-23', 'дата', 'Допущение',
     '«На днях придёт 220 000 ₽» (20.10); пришло — дата больше не нужна', '2026-10-20', 'миграция'),
    -- Цели и лимиты KPI: линии на графиках и цвет плиток.
    ('kpi_cac_goal', 'Цель: CAC не выше', 500, null, '₽', 'Решение 20.10', 'Линия цели на графике CAC', '2026-10-20', 'миграция'),
    ('kpi_cac_limit', 'Лимит: CAC не выше', 700, null, '₽', 'Решение 20.10', 'Как ворота недели 6 (gate_w6_cac_max)', '2026-10-20', 'миграция'),
    ('kpi_avg_check_goal', 'Цель: средний чек', 1490, null, '₽', 'Решение 20.10', 'Цена набора «Точка входа»', '2026-10-20', 'миграция'),
    -- «Требует внимания».
    ('card_unreconciled_days', 'Покупки с карты: сколько дней ждать разноски', 3, null, 'дней', 'Решение 20.10',
     'Перевод себе на покупки без записи «мимо счёта» дольше — строка в «Требует внимания»', '2026-10-20', 'миграция'),
    ('claude_unaccounted_max', 'Claude: допустимая доля неучтённого за 7 дней', 0.20, null, '%', 'Решение 20.10',
     'Счёт Anthropic против учёта бота', '2026-10-20', 'миграция'),
    ('tea_lot_warn_days', 'Партия чая: за сколько дней до старта напоминать', 21, null, 'дней', 'Решение 20.10',
     'Партия закупки не внесена, а до старта меньше — строка в «Требует внимания»', '2026-10-20', 'миграция')
) as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)
where not exists (select 1 from fin.inputs i where i.key = v.key);

-- --- статьи бюджета пилота и как к ним сводятся выписка и «мимо счёта» ------------------

-- D3 / D6: статьи бюджета пилота (fin.v_pilot_budget) — одни для выписки и для
-- «мимо счёта». Вид денег (cash_class) — для ДДС: оборудование — вложения.
-- kind: разовая — тратится до старта; постоянная — помесячно (сервисы, банк,
-- подрядчик, взносы); реклама — по плану рекламы. Цвет — один и тот же у статьи
-- на всех графиках.
create table if not exists fin.budget_articles (
    item        text primary key,
    line_no     integer,
    kind        text not null check (kind in ('разовая', 'постоянная', 'реклама', 'нет')),
    cash_class  text not null default 'Операционные'
                check (cash_class in ('Операционные', 'Вложения', 'Финансирование', 'Переводы между своими счетами')),
    color       text,
    sort        integer not null
);
insert into fin.budget_articles (item, line_no, kind, cash_class, color, sort) values
    ('Первая закупка чая', 1, 'разовая', 'Операционные', '#7A9A3A', 1),
    ('Упаковка и расходники (лимит)', 2, 'разовая', 'Операционные', '#C9A227', 2),
    ('Термопринтер', 3, 'разовая', 'Вложения', '#4C78A8', 3),
    ('ВК: пополнение кабинета', 4, 'реклама', 'Операционные', '#9C6ADE', 4),
    ('Подрядчик за пилот', 5, 'постоянная', 'Операционные', '#B279A2', 5),
    ('Доставка основной партии', 6, 'разовая', 'Операционные', '#8C6D31', 6),
    ('Весы', 7, 'разовая', 'Вложения', '#2F6690', 7),
    ('ККТ, ФН, фискализация', 8, 'разовая', 'Вложения', '#5D7A99', 8),
    ('УКЭП / ЭДО / маркировка', 9, 'разовая', 'Операционные', '#A0522D', 9),
    ('GTIN / карточки товаров', 10, 'разовая', 'Операционные', '#BC8F6F', 10),
    ('Сервисы за пилот', 11, 'постоянная', 'Операционные', '#6B8E9E', 11),
    ('Взносы 2026: расчётный резерв', 12, 'постоянная', 'Операционные', '#7F7F7F', 12),
    ('Обслуживание банка за пилот', 13, 'постоянная', 'Операционные', '#A9A9A9', 13),
    ('API тестов и консультаций до продаж', 14, 'постоянная', 'Операционные', '#5F9EA0', 14),
    ('Тестовые отправки до рекламы', 15, 'разовая', 'Операционные', '#D08C60', 15),
    ('Не сопоставлено', null, 'нет', 'Операционные', '#BDB76B', 99)
on conflict (item) do nothing;
comment on table fin.budget_articles is
    'Статьи бюджета пилота (fin.v_pilot_budget): вид траты, вид денег для ДДС, цвет на графиках. «Не сопоставлено» — то, что ни на какую статью не легло.';

-- Соответствие: статья выписки (article_id cashflow) или категория формы
-- «Добавить расход» → статья бюджета. match_text — шаблон LIKE по описанию
-- расхода строчными буквами (только «мимо счёта»: назначение платежа банка
-- наружу не выходит).
-- Срабатывает строка с меньшим priority.
create table if not exists fin.budget_article_map (
    id           bigserial primary key,
    source       text not null check (source in ('выписка', 'мимо счёта')),
    match_key    text not null,
    match_text   text,
    budget_item  text not null references fin.budget_articles (item) on update cascade,
    priority     integer not null default 100,
    note         text
);
insert into fin.budget_article_map (source, match_key, match_text, budget_item, priority, note)
select * from (values
    ('выписка', 'cfo_out_goods', null::text, 'Первая закупка чая', 100, null::text),
    ('выписка', 'cfo_out_packaging', null, 'Упаковка и расходники (лимит)', 100, null),
    ('выписка', 'cfo_out_marking', null, 'УКЭП / ЭДО / маркировка', 100, null),
    ('выписка', 'cfo_out_cdek', null, 'Тестовые отправки до рекламы', 100, 'До старта отправки — тестовые'),
    ('выписка', 'cfo_out_ozon_services', null, 'Тестовые отправки до рекламы', 100, 'До старта отправки — тестовые'),
    ('выписка', 'cfo_out_vk_ads', null, 'ВК: пополнение кабинета', 100, null),
    ('выписка', 'cfo_out_ads_contractor', null, 'Подрядчик за пилот', 100, null),
    ('выписка', 'cfo_out_services', null, 'Сервисы за пилот', 100, null),
    ('выписка', 'cfo_out_bank_fees', null, 'Обслуживание банка за пилот', 100, null),
    ('выписка', 'cfo_out_acquiring_fees', null, 'Обслуживание банка за пилот', 100, null),
    ('выписка', 'cfo_out_taxes', null, 'Взносы 2026: расчётный резерв', 100, null),
    ('выписка', 'cfi_equipment', null, 'Не сопоставлено', 100, 'Какое оборудование — разметкой операции'),
    ('мимо счёта', 'закупка', null, 'Первая закупка чая', 100, null),
    ('мимо счёта', 'логистика', null, 'Доставка основной партии', 100, null),
    ('мимо счёта', 'упаковка', null, 'Упаковка и расходники (лимит)', 100, null),
    ('мимо счёта', 'оборудование', '%принтер%', 'Термопринтер', 10, null),
    ('мимо счёта', 'оборудование', '%вес%', 'Весы', 10, null),
    ('мимо счёта', 'оборудование', '%ккт%', 'ККТ, ФН, фискализация', 10, null),
    ('мимо счёта', 'оборудование', null, 'Не сопоставлено', 100, null),
    ('мимо счёта', 'реклама', null, 'ВК: пополнение кабинета', 100, null),
    ('мимо счёта', 'подрядчик', null, 'Подрядчик за пилот', 100, null),
    ('мимо счёта', 'сервисы', null, 'Сервисы за пилот', 100, null),
    ('мимо счёта', 'налоги_взносы', null, 'Взносы 2026: расчётный резерв', 100, null),
    ('мимо счёта', 'банк', null, 'Обслуживание банка за пилот', 100, null),
    ('мимо счёта', 'маркировка', null, 'УКЭП / ЭДО / маркировка', 100, null)
) as v (source, match_key, match_text, budget_item, priority, note)
where not exists (select 1 from fin.budget_article_map);
comment on table fin.budget_article_map is
    'Статья выписки или категория «мимо счёта» → статья бюджета пилота. Нет строки — «Не сопоставлено».';

-- D2 / D3: ручная разметка операции выписки поверх статьи cashflow. Последняя
-- строка по операции действует, прежние остаются историей. transfer_kind — для
-- переводов себе: без разметки перевод на свою карту считается «на покупки».
create table if not exists fin.bank_marks (
    id             bigserial primary key,
    operation_id   text        not null,
    transfer_kind  text check (transfer_kind in ('на покупки', 'вывод владельцем', 'между своими счетами')),
    budget_item    text references fin.budget_articles (item) on update cascade,
    note           text,
    entered_by     text        not null check (fin.valid_author(entered_by)),
    created_at     timestamptz not null default now(),
    constraint bank_marks_something check (transfer_kind is not null or budget_item is not null)
);
create index if not exists bank_marks_operation_idx on fin.bank_marks (operation_id, created_at desc);
comment on table fin.bank_marks is
    'Разметка операции выписки: вид перевода себе и/или статья бюджета. Последняя строка по operation_id действует.';

-- D3: термопринтер Xprinter XP-365B, 4 831 ₽ со счёта ИП 06.10.2026 (в cashflow —
-- «Прочие операционные», в fin.expenses — оборудование). Операция есть только на
-- бою; в другой базе строка просто ни с чем не совпадёт.
insert into fin.bank_marks (operation_id, budget_item, note, entered_by)
select 'b2ef7c61-2ed8-007f-9544-e6e07ce16161', 'Термопринтер',
       'Термопринтер 4 831 ₽ (Ozon) — оборудование, во вложения', 'миграция'
where not exists (select 1 from fin.bank_marks where operation_id = 'b2ef7c61-2ed8-007f-9544-e6e07ce16161');

-- D9: одно имя товара — как в каталоге бота и fin, без кода поставщика.
create table if not exists fin.product_names (
    product        text primary key,
    display_name   text not null,
    supplier_code  text
);
insert into fin.product_names (product, display_name, supplier_code)
select p.product,
       case trim(regexp_replace(p.product, '\s*\(([^()]*)\)\s*$', ''))
            -- «Дянь Хунь» в прайсе поставщика; в каталоге бота и справочниках — «Дянь Хун».
            when 'Дянь Хунь фермерский' then 'Дянь Хун фермерский'
            else trim(regexp_replace(p.product, '\s*\(([^()]*)\)\s*$', '')) end,
       nullif(substring(p.product from '\(([^()]*)\)\s*$'), '')
from (select sku_name as product from fin.sku union select bundle from fin.bundles) p
-- «Набор «Точка входа»» — скобок нет, имя то же.
on conflict (product) do nothing;
comment on table fin.product_names is 'Имя товара для дашбордов: без кода поставщика; код — отдельным полем.';

-- --- D2 / D3: операции выписки с разметкой ----------------------------------------------

create or replace view fin.v_bank_ops as
with m as (
    select distinct on (operation_id) operation_id, transfer_kind, budget_item
    from fin.bank_marks order by operation_id, created_at desc
),
ops as (
    select f.operation_id, f.split_no, f.operation_date as day, f.direction, f.amount,
           case when f.direction = 'in' then f.amount else -f.amount end as amount_signed,
           f.article_id, a.article, a.section, a.is_capex,
           m.transfer_kind as marked_kind, m.budget_item as marked_item
    from finance.fact_operations f
    join finance.articles a using (article_id)
    left join m using (operation_id)
)
select o.operation_id, o.split_no, o.day, o.direction, o.amount, o.amount_signed, o.article_id, o.article, o.section,
       -- Перевод себе: без разметки — «на покупки» (покупка потом вносится «мимо счёта»).
       case when o.article_id = 'tech_internal_transfer' and o.direction = 'out'
            then coalesce(o.marked_kind, 'на покупки') end as transfer_kind,
       coalesce(o.marked_item, bm.budget_item,
                case when o.direction = 'out' and o.section in ('CFO', 'CFI') or o.article_id = 'tech_unclassified'
                     then 'Не сопоставлено' end) as budget_item,
       case when o.article_id = 'tech_internal_transfer'
                 then case when o.direction = 'out' and coalesce(o.marked_kind, 'на покупки') = 'вывод владельцем'
                           then 'Финансирование' else 'Переводы между своими счетами' end
            when o.section = 'CFF' or o.article_id = 'tech_loan_payment' then 'Финансирование'
            when o.section = 'CFI' or o.is_capex or ba.cash_class = 'Вложения' then 'Вложения'
            else 'Операционные' end as cash_class,
       o.marked_item is not null or o.marked_kind is not null as is_marked
from ops o
left join lateral (
    select x.budget_item from fin.budget_article_map x
    where x.source = 'выписка' and x.match_key = o.article_id and x.match_text is null
    order by x.priority, x.id limit 1
) bm on true
left join fin.budget_articles ba on ba.item = coalesce(o.marked_item, bm.budget_item);
comment on view fin.v_bank_ops is
    'Операции выписки без контрагентов и назначений: статья cashflow, разметка fin.bank_marks, статья бюджета, вид денег (Операционные / Вложения / Финансирование / Переводы между своими счетами).';

-- Расходы бизнеса (D2): выписка без переводов себе и финансирования + записи
-- «мимо счёта». Перевод себе на покупки сюда не входит — иначе покупка с карты
-- посчиталась бы дважды; его сверяет fin.v_card_reconcile.
create or replace view fin.v_spend as
select 'выписка'::text as source, b.day, b.amount, b.budget_item, b.cash_class, b.article as what, b.operation_id::text as ref
from fin.v_bank_ops b
where b.direction = 'out' and b.cash_class in ('Операционные', 'Вложения')
union all
select 'мимо счёта', x.date, x.amount, coalesce(mm.budget_item, 'Не сопоставлено'),
       coalesce(ba.cash_class, case when x.category = 'оборудование' then 'Вложения' else 'Операционные' end),
       x.category, 'expense:' || x.id
from fin.expenses x
left join lateral (
    select m.budget_item from fin.budget_article_map m
    where m.source = 'мимо счёта' and m.match_key = x.category
      -- Регистр — переводом, а не ILIKE: у ILIKE кириллица зависит от локали базы.
      and (m.match_text is null
           or translate(coalesce(x.description, ''), 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',
                        'абвгдеёжзийклмнопрстуфхцчшщъыьэюя') like m.match_text)
    order by m.priority, m.id limit 1
) mm on true
left join fin.budget_articles ba on ba.item = coalesce(mm.budget_item, 'Не сопоставлено')
where not x.is_plan and x.paid_from = 'мимо счёта';
comment on view fin.v_spend is
    'Расходы бизнеса: выписка (без переводов себе и финансирования) + записи «мимо счёта», со статьёй бюджета и видом денег.';

-- Сверка покупок с карты (D2): переводы себе «на покупки» против записей «мимо
-- счёта» по месяцам и нарастающим итогом. «Не разнесено» > 0 — деньги ушли на
-- карту, а покупка не внесена; < 0 — внесено больше, чем переведено.
create or replace view fin.v_card_reconcile as
with t as (
    select date_trunc('month', day)::date as month, sum(amount) as transferred, max(day) as last_transfer
    from fin.v_bank_ops where transfer_kind = 'на покупки' group by 1
),
x as (
    select date_trunc('month', date)::date as month, sum(amount) as recorded
    from fin.expenses where not is_plan and paid_from = 'мимо счёта' group by 1
)
select month, coalesce(t.transferred, 0) as transferred, coalesce(x.recorded, 0) as recorded,
       coalesce(t.transferred, 0) - coalesce(x.recorded, 0) as unreconciled,
       sum(coalesce(t.transferred, 0) - coalesce(x.recorded, 0)) over (order by month) as unreconciled_total,
       t.last_transfer
from t full join x using (month);
comment on view fin.v_card_reconcile is
    'Переводы себе на покупки против покупок «мимо счёта» по месяцам: не разнесено за месяц и нарастающим итогом.';

create or replace view fin.v_card_reconcile_status as
select coalesce(sum(transferred), 0) as transferred, coalesce(sum(recorded), 0) as recorded,
       coalesce(sum(transferred), 0) - coalesce(sum(recorded), 0) as unreconciled,
       max(last_transfer) as last_transfer,
       -- «Дольше N дней»: последний перевод на покупки старше N дней, а разница не закрыта.
       coalesce(sum(transferred), 0) <> coalesce(sum(recorded), 0)
           and max(last_transfer) <= current_date - coalesce(fin.inp('card_unreconciled_days')::int, 3) as overdue
from fin.v_card_reconcile;

-- --- D1 / D13: движение денег по месяцам ----------------------------------------------

create or replace view fin.v_cash_monthly as
with months as (
    select generate_series(date_trunc('month', fin.inp_date('ip_registration_date')),
                           date_trunc('month', current_date), interval '1 month')::date as month
),
f as (
    select date_trunc('month', day)::date as month,
           sum(amount_signed) filter (where cash_class = 'Операционные') as operating,
           sum(amount_signed) filter (where cash_class = 'Вложения') as investing,
           sum(amount_signed) filter (where cash_class = 'Финансирование') as financing,
           sum(amount_signed) filter (where cash_class = 'Переводы между своими счетами') as transfers,
           sum(amount_signed) filter (where article_id in ('cff_loan_received', 'cff_loan_principal')) as loans
    from fin.v_bank_ops group by 1
)
select m.month,
       coalesce(f.operating, 0) as operating, coalesce(f.investing, 0) as investing,
       coalesce(f.financing, 0) as financing, coalesce(f.transfers, 0) as transfers, coalesce(f.loans, 0) as loans,
       -- Как FCF cashflow: операционные + вложения.
       coalesce(f.operating, 0) + coalesce(f.investing, 0) as free_flow,
       -- D1: среднее по доступным месяцам, не больше трёх (было — сумма за три).
       avg(coalesce(f.operating, 0) + coalesce(f.investing, 0))
           over (order by m.month rows between 2 preceding and current row) as free_flow_avg_3m,
       (select balance from finance.v_daily_balance_total
         where day = least((m.month + interval '1 month - 1 day')::date,
                           (select max(day) from finance.v_daily_balance_total))) as closing_balance
from months m left join f using (month);
comment on view fin.v_cash_monthly is
    'Деньги по месяцам по выписке: операционные, вложения, финансирование, переводы себе; поток = операционные + вложения; среднее за 3 месяца — по доступным месяцам.';

-- ДДС (D13): вид денег × статья × месяц, целые рубли.
create or replace view fin.v_ddc as
select date_trunc('month', day)::date as month, cash_class,
       case when cash_class = 'Переводы между своими счетами' and transfer_kind is not null
                then 'Перевод себе: ' || transfer_kind
            when budget_item is not null and budget_item <> 'Не сопоставлено' and direction = 'out'
                then budget_item
            else article end as item,
       round(sum(amount_signed)) as amount
from fin.v_bank_ops group by 1, 2, 3;
comment on view fin.v_ddc is 'ДДС по выписке: вид денег, статья (статья бюджета для расходов), месяц, целые рубли.';

-- --- D5: вклады партнёров -------------------------------------------------------------
-- Взнос виден в выписке («Взнос собственника»); форма «Вклад / выплата партнёра»
-- говорит, чей он. Внесено = больше из двух: выписка или размеченное формой (вклад
-- мимо счёта форма добавляет сверх выписки). Неразмеченный остаток — «Не указано, чей».
create or replace view fin.v_partner_capital as
with bank as (select coalesce(sum(amount), 0) as bank_in from fin.v_bank_ops where article_id = 'cff_owner_contribution'),
marked as (
    select partner, coalesce(sum(amount) filter (where kind = 'вклад'), 0) as contributed,
           coalesce(sum(amount) filter (where kind = 'выплата'), 0) as paid_out
    from fin.capital group by partner
),
partners (partner, plan_key, sort) as (values ('Никита', 'capital_nikita', 1), ('Илья', 'capital_ilya', 2))
select p.partner, p.sort, fin.inp(p.plan_key) as plan, coalesce(m.contributed, 0) as contributed,
       coalesce(m.paid_out, 0) as paid_out
from partners p left join marked m using (partner)
union all
select 'Не указано, чей', 3, null,
       greatest(0, b.bank_in - (select coalesce(sum(contributed), 0) from marked)), 0
from bank b
where b.bank_in > (select coalesce(sum(contributed), 0) from marked);
comment on view fin.v_partner_capital is
    'Вклады по партнёрам: план (capital_nikita / capital_ilya), внесено по форме, неразмеченный остаток взносов по выписке.';

create or replace view fin.v_capital_status as
select fin.inp('capital_plan') as plan,
       sum(contributed) as contributed,
       greatest(0, fin.inp('capital_plan') - sum(contributed)) as remaining,
       sum(contributed) / nullif(fin.inp('capital_plan'), 0) as share
from fin.v_partner_capital;

-- --- D6: бюджет пилота — план, потрачено, осталось -------------------------------------
create or replace view fin.v_pilot_budget_status as
with spent as (
    select budget_item, sum(amount) as spent from fin.v_spend
    where day >= fin.inp_date('ip_registration_date')
      and day < coalesce(fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1), 'infinity'::date)
    group by 1
)
select b.line_no, b.item, ba.kind, b.plan_amount as plan, coalesce(s.spent, 0) as spent,
       case when b.plan_amount is not null then b.plan_amount - coalesce(s.spent, 0) end as left_amount,
       coalesce(s.spent, 0) / nullif(b.plan_amount, 0) as spent_share, ba.sort, ba.color
from fin.v_pilot_budget b
join fin.budget_articles ba on ba.item = b.item
left join spent s on s.budget_item = b.item
union all
select null, 'Не сопоставлено', 'нет', null, s.spent, null, null, 99, '#BDB76B'
from spent s where s.budget_item = 'Не сопоставлено' and s.spent > 0;
comment on view fin.v_pilot_budget_status is
    'Бюджет пилота по статьям: план (fin.v_pilot_budget), потрачено (fin.v_spend), осталось. «Не сопоставлено» — отдельной строкой.';

-- --- D4: деньги сейчас и прогноз ------------------------------------------------------
-- Прогноз по дням до конца пилота (не меньше 8 недель): остаток по выписке минус
-- холды + оставшиеся вклады по плану + выплаты ЮKassa в пути (fin.v_cash_forecast_daily)
-- − оставшиеся разовые траты бюджета (план − потрачено, поровну от прихода вкладов до старта)
-- − реклама по плану (М1–М3, поровну по дням месяца) − постоянные: сервисы каждый
-- день, банк с bank_paid_from, подрядчик в начале месяца пилота, налоги и взносы
-- по налоговому календарю. Плановые расходы формы сюда не входят: план — бюджет.
create or replace view fin.v_runway_daily as
with s as (
    select (select balance from finance.v_daily_balance_total order by day desc limit 1)
           - coalesce((select sum(holds_out) from finance.v_pending_summary), 0) as balance,
           fin.inp_date('pilot_start_date') as pilot_start,
           fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1) as pilot_end
),
capital as (
    select c.remaining as amount,
           greatest(coalesce(fin.inp_date('capital_expected_date'), current_date + 1), current_date + 1) as day
    from fin.v_capital_status c
),
h as (
    -- Разовые траты бюджета идут на вклады: пока вклады по плану не пришли, их не
    -- начинаем — иначе прогноз «уходит ниже резерва» за день до прихода денег.
    select greatest(s.pilot_end + 30, current_date + 56) as horizon,
           case when c.amount > 0 then c.day else current_date + 1 end as one_off_from,
           greatest(s.pilot_start - 1, case when c.amount > 0 then c.day else current_date + 1 end + 6) as one_off_until
    from s cross join capital c
),
days as (select d::date as day from h, generate_series(current_date + 1, h.horizon, interval '1 day') d),
one_off as (
    select coalesce(sum(greatest(coalesce(left_amount, 0), 0)), 0) as amount
    from fin.v_pilot_budget_status where kind = 'разовая'
),
months as (
    select n, fin.pilot_month_start(n) as m_start, fin.pilot_month_start(n + 1) as m_end,
           fin.inp('ad_plan_m' || n) as ads
    from generate_series(1, coalesce(fin.inp('pilot_months')::int, 3)) n
),
taxes as (
    select due_date as day, sum(amount_hint) as amount from fin.tax_calendar
    where status = 'ожидает' and kind in ('взносы', 'взносы_1%', 'налог', 'аванс') group by due_date
),
flows as (
    select d.day,
           case when d.day = c.day then c.amount else 0 end as capital_in,
           coalesce(p.payouts_in, 0) as payouts_in,
           case when d.day between h.one_off_from and h.one_off_until
                then o.amount / (h.one_off_until - h.one_off_from + 1) else 0 end as budget_out,
           coalesce((select m.ads / (m.m_end - m.m_start) from months m where d.day >= m.m_start and d.day < m.m_end), 0)
               as ads_out,
           coalesce(fin.inp('infra_monthly'), 0) * 12 / 365.0
             + case when extract(day from d.day) = 1 and d.day >= fin.inp_date('bank_paid_from')
                    then coalesce(fin.inp('bank_fee_monthly'), 0) else 0 end
             + case when d.day in (select m_start from months) then coalesce(fin.contractor_monthly(), 0) else 0 end
             + coalesce(t.amount, 0) as fixed_out
    from days d cross join h cross join one_off o cross join capital c
    left join fin.v_cash_forecast_daily p on p.day = d.day
    left join taxes t on t.day = d.day
)
select f.day, f.capital_in, f.payouts_in, f.budget_out, f.ads_out, f.fixed_out,
       s.balance + sum(f.capital_in + f.payouts_in - f.budget_out - f.ads_out - f.fixed_out) over (order by f.day) as closing,
       fin.inp('min_cash_reserve') as reserve
from flows f cross join s;
comment on view fin.v_runway_daily is
    'Прогноз денег по дням до конца пилота + 30 дней: вклады по плану, ЮKassa в пути, оставшийся бюджет пилота, реклама по плану, постоянные расходы и налоги. Продолжает fin.v_cash_forecast_daily теми же вводными.';

-- Остаток по дням для графика: факт по выписке и прогноз одной таблицей.
create or replace view fin.v_balance_chart as
select day, balance as actual, null::numeric as forecast, fin.inp('min_cash_reserve') as reserve
from finance.v_daily_balance_total
where day >= fin.inp_date('ip_registration_date')
union all
-- Прогноз начинается с последнего факта, чтобы линии сходились.
select day, null, balance, fin.inp('min_cash_reserve')
from finance.v_daily_balance_total where day = (select max(day) from finance.v_daily_balance_total)
union all
select day, null, closing, reserve from fin.v_runway_daily;

create or replace view fin.v_money_now as
with b as (
    select day, balance from finance.v_daily_balance_total order by day desc limit 1
),
r as (select min(day) as runout from fin.v_runway_daily where closing < reserve),
hz as (select max(day) as horizon from fin.v_runway_daily)
select b.day as bank_date, b.balance as bank_balance, fin.inp('min_cash_reserve') as reserve,
       b.balance - fin.inp('min_cash_reserve') as free_cash,
       r.runout as runout_date, hz.horizon,
       fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1) as pilot_end
from b cross join r cross join hz;
comment on view fin.v_money_now is
    'Свободно сверх резерва = остаток по выписке − минимальный резерв; runout_date — первый день прогноза ниже резерва (пусто — не до конца горизонта).';

create or replace view fin.v_weekly_forecast as
with w as (
    select ((day - current_date - 1) / 7) + 1 as week_no, min(day) as week_start,
           sum(capital_in + payouts_in) as inflow, sum(budget_out + ads_out + fixed_out) as outflow,
           (array_agg(closing order by day desc))[1] as closing, min(closing) as lowest, max(reserve) as reserve
    from fin.v_runway_daily group by 1
)
select week_no, week_start, closing - inflow + outflow as opening, inflow, outflow, closing, lowest,
       lowest < reserve as below_reserve
from w where week_no <= 8 order by week_no;

-- --- D15: статус пилота -------------------------------------------------------------------
create or replace view fin.v_pilot_status as
select fin.inp_date('pilot_start_date') as start_date,
       fin.inp_date('pilot_start_date') - current_date as days_to_start,
       current_date >= fin.inp_date('pilot_start_date') as started,
       c.pilot_loss, c.pilot_pause_loss, c.pilot_max_loss, c.pilot_status
from fin.v_cash_status c;

-- --- D7: безубыточность М1–М3 -----------------------------------------------------------
create or replace view fin.v_breakeven_plan as
with lead as (
    select u.product, u.margin_before_cac as margin from fin.v_unit_cost u
    where u.product = (select value_text from fin.inputs_current where key = 'lead_product')
)
select m.month_no, m.month_label, m.period_start,
       fin.fixed_month(m.period_start) + coalesce(fin.contractor_monthly(), 0) as fixed_costs,
       fin.inp('ad_plan_m' || m.month_no) as ads_plan,
       l.product as lead_product, l.margin as lead_margin,
       case when l.margin > 0 and fin.inp('ad_plan_m' || m.month_no) is not null
            then ceil((fin.fixed_month(m.period_start) + coalesce(fin.contractor_monthly(), 0)
                       + fin.inp('ad_plan_m' || m.month_no)) / l.margin)::int end as orders_needed
from fin.v_pilot_months m cross join lead l
where m.month_no between 1 and coalesce(fin.inp('pilot_months')::int, 3);
comment on view fin.v_breakeven_plan is
    'Заказов в месяц для безубыточности = ⌈(постоянные + подрядчик + реклама по плану) / маржа до рекламы lead-продукта⌉, М1–М3.';

create or replace view fin.v_breakeven_scenarios as
select b.month_no, b.month_label, sc.ads,
       case when b.lead_margin > 0 then ceil((b.fixed_costs + sc.ads) / b.lead_margin)::int end as orders_needed
from fin.v_breakeven_plan b
cross join lateral (
    select trim(x)::numeric as ads
    from unnest(string_to_array((select value_text from fin.inputs_current where key = 'ad_scenarios'), ',')) x
    where trim(x) ~ '^[0-9]+(\.[0-9]+)?$'
) sc;

-- --- D8 / D9 / D10: товары и маржа --------------------------------------------------------
create or replace view fin.v_product_card as
select u.product, coalesce(n.display_name, u.product) as name, n.supplier_code, u.kind,
       case u.kind when 'набор' then 1 when 'сет' then 2 when 'пачка' then 3 else 4 end as kind_sort,
       case u.kind when 'набор' then 'Наборы' when 'сет' then 'Сеты' when 'пачка' then 'Пачки' else 'Блины' end as kind_group,
       u.retail_price, u.full_cost, u.margin_before_cac, u.margin_pct,
       u.margin_pct < coalesce(fin.inp('low_margin_threshold'), 0.20) as low_margin,
       u.bot_name is not null as sold_by_bot
from fin.v_unit_cost u left join fin.product_names n using (product);
comment on view fin.v_product_card is
    'Товар для дашборда: имя без кода поставщика, тип, маржа до рекламы (fin.v_unit_cost), флаг низкой маржи (low_margin_threshold), продаёт ли бот.';

-- Водопад «из чего сложилась маржа до рекламы» на единицу товара (fin.v_unit_cost).
create or replace view fin.v_unit_waterfall as
with u as (
    select c.*, fin.inp('usn_rate') + fin.payment_rate() as fees, fin.api_per_new_buyer() as api
    from fin.v_unit_cost c
)
select product, step_no, step, amount from u
cross join lateral (values
    (1, 'Цена', u.retail_price),
    (2, 'УСН и эквайринг', -u.retail_price * u.fees),
    (3, 'Чай', -(u.tea_cost + u.inbound_cost)),
    (4, 'Упаковка и коды', -(u.pouch_cost + u.codes_cost + u.labels_cost + u.box_cost)),
    (5, 'Доставка и прочее на заказ', -(u.order_extra_cost - u.api)),
    (6, 'Claude', -u.api)
) as w (step_no, step, amount);
comment on view fin.v_unit_waterfall is
    'Маржа до рекламы на единицу по шагам: цена − УСН и эквайринг − чай − упаковка и коды − доставка и прочее − Claude. Сумма шагов = fin.v_unit_cost.margin_before_cac.';

-- --- D12: Claude в рублях, сверка со счётом ----------------------------------------------
create or replace view fin.v_claude_daily as
with bill as (select day, sum(amount_usd) as usd from analytics.v_anthropic_costs group by 1),
bot as (select (at at time zone 'UTC')::date as day, sum(cost_usd) as usd from analytics.v_llm_usage group by 1)
select coalesce(bill.day, bot.day) as day,
       bill.usd * fin.inp('usd_rub') as bill_rub, bot.usd * fin.inp('usd_rub') as bot_rub,
       (coalesce(bill.usd, 0) - coalesce(bot.usd, 0)) * fin.inp('usd_rub') as unaccounted_rub,
       bot.day is not null as bot_counted
from bill full join bot on bot.day = bill.day;
comment on view fin.v_claude_daily is
    'Claude по дням (UTC) в рублях по курсу usd_rub: счёт Anthropic, учёт бота, неучтённое.';

-- Доля неучтённого за 7 дней — только по дням, когда бот вёл учёт и счёт уже пришёл
-- (счёт Anthropic приходит с задержкой; до 18.10.2026 учёта бота нет вовсе).
create or replace view fin.v_claude_status as
select sum(bill_rub) as bill_rub, sum(bot_rub) as bot_rub,
       (sum(bill_rub) - sum(bot_rub)) / nullif(sum(bill_rub), 0) as unaccounted_share,
       count(*) as days
from fin.v_claude_daily
where day >= current_date - 6 and bot_counted and bill_rub is not null;

-- --- D14: P&L статьями -------------------------------------------------------------------
create or replace view fin.v_pnl_report as
with p as (select * from fin.v_pnl_monthly where month_no <= coalesce(fin.inp('pilot_months')::int, 3))
select l.sort, l.item, l.is_total,
       max(l.v) filter (where p.month_no = 0) as m0,
       max(l.v) filter (where p.month_no = 1) as m1,
       max(l.v) filter (where p.month_no = 2) as m2,
       max(l.v) filter (where p.month_no = 3) as m3
from p cross join lateral (values
    (1, 'Заказы, шт.', false, p.orders::numeric),
    (2, 'Выручка', false, p.revenue),
    (3, 'Чай', false, -p.cogs_tea),
    (4, 'Упаковка и коды', false, -p.packaging_and_codes),
    (5, 'Доставка за наш счёт', false, -p.delivery_subsidy),
    (6, 'Возвраты', false, -p.returns),
    (7, 'Эквайринг', false, -p.acquiring),
    (8, 'Маржа до рекламы', true, p.margin_before_cac),
    (9, 'Реклама (свои)', false, -p.ads),
    (10, 'Подрядчик', false, -p.contractor),
    (11, 'Сервисы, банк, Claude и прочее', false, -p.other_opex),
    (12, 'Резерв УСН', false, -p.usn_reserve),
    (13, 'ОПЕРАЦИОННАЯ ПРИБЫЛЬ', true, p.operating_profit)
) as l (sort, item, is_total, v)
group by l.sort, l.item, l.is_total;
comment on view fin.v_pnl_report is 'P&L статьями: строки — статьи, столбцы — М0 (до пилота), М1–М3. Расходы со знаком минус.';

-- --- U5: проценты в текстах — с запятой, как в остальных числах («12,5 %») -------------
create or replace function fin.fmt_pct(x numeric) returns text
    language sql immutable as $$
    select case when x is null then 'нет данных' else replace(to_char(x * 100, 'FM9990.0'), '.', ',') || ' %' end
$$;

-- --- D3: P&L без вложений ------------------------------------------------------------
-- Прочие операционные по выписке — только то, что fin.v_bank_ops считает
-- операционным: термопринтер (cfo_out_other, размечен как вложение) из прибыли
-- уходит. Остальное — как в 006, столбцы те же.
create or replace view fin.v_pnl_base as
with m as (select * from fin.v_pilot_months),
orders as (
    select m.month_no, count(*) as orders, count(*) filter (where e.is_first_order) as new_clients,
           sum(e.revenue) as revenue, sum(e.cogs_tea) as cogs_tea, sum(e.packaging_and_codes) as packaging_and_codes,
           sum(e.delivery_subsidy) as delivery_subsidy, sum(e.revenue) filter (where e.is_refunded) as returns,
           sum(e.acquiring) as acquiring,
           count(*) filter (where e.is_first_order) as first_orders,
           sum(e.margin_before_cac + e.usn_reserve) filter (where e.is_first_order) as first_orders_margin_before_usn,
           sum(e.carrier_return_cost) as carrier_returns
    from fin.v_order_economics e
    join m on e.paid_date_msk >= m.period_start and e.paid_date_msk < m.period_end
    group by m.month_no
),
ads as (
    select m.month_no, sum(a.spend_own) as ads, sum(a.spend_bonus) as ads_bonus
    from fin.ad_spend a join m on a.date >= m.period_start and a.date < m.period_end group by m.month_no
),
bank as (
    select m.month_no,
           sum(b.amount) filter (where b.article_id = 'cfo_out_ads_contractor') as contractor,
           sum(b.amount) filter (where b.article_id in ('cfo_out_services', 'cfo_out_bank_fees', 'cfo_out_other')
                                   and b.cash_class = 'Операционные') as other_opex
    from fin.v_bank_ops b join m on b.day >= m.period_start and b.day < m.period_end
    where b.direction = 'out' group by m.month_no
),
manual as (
    select m.month_no,
           sum(x.amount) filter (where x.category = 'подрядчик') as contractor,
           sum(x.amount) filter (where x.category in ('сервисы', 'банк', 'прочее')) as other_opex
    from fin.expenses x join m on x.date >= m.period_start and x.date < m.period_end
    where not x.is_plan and x.paid_from = 'мимо счёта' group by m.month_no
),
api as (
    select m.month_no, sum(c.amount_usd) * fin.inp('usd_rub') as claude_api
    from analytics.v_anthropic_costs c join m on c.day >= m.period_start and c.day < m.period_end group by m.month_no
),
p as (
    select m.month_no, m.month_label, m.period_start, m.period_end, m.period_start <= current_date as started,
           o.orders, o.new_clients, o.revenue, o.cogs_tea, o.packaging_and_codes, o.delivery_subsidy, o.returns,
           o.acquiring, o.first_orders, o.first_orders_margin_before_usn, o.carrier_returns,
           a.ads, a.ads_bonus,
           coalesce(b.contractor, 0) + coalesce(x.contractor, 0) as contractor,
           coalesce(b.other_opex, 0) + coalesce(x.other_opex, 0) + coalesce(c.claude_api, 0) as other_opex,
           c.claude_api,
           fin.usn_reserve_between(m.period_start, least(m.period_end, current_date + 1)) as usn_reserve,
           (select sum(income) from fin.v_kudir k where k.income_date >= m.period_start and k.income_date < m.period_end)
               as usn_income,
           (select sum(paid_amount) from fin.v_contributions_paid w
             where w.done_on >= m.period_start and w.done_on < m.period_end) as contributions_paid
    from m left join orders o using (month_no) left join ads a using (month_no) left join bank b using (month_no)
    left join manual x using (month_no) left join api c using (month_no)
)
select p.month_no, p.month_label, p.period_start, p.period_end,
       case when p.started then coalesce(p.orders, 0) end as orders,
       case when p.started then coalesce(p.new_clients, 0) end as new_clients,
       case when p.started then coalesce(p.revenue, 0) end as revenue,
       case when p.started then coalesce(p.cogs_tea, 0) end as cogs_tea,
       case when p.started then coalesce(p.packaging_and_codes, 0) end as packaging_and_codes,
       case when p.started then coalesce(p.delivery_subsidy, 0) + coalesce(p.carrier_returns, 0) end as delivery_subsidy,
       case when p.started then coalesce(p.returns, 0) end as returns,
       case when p.started then coalesce(p.acquiring, 0) end as acquiring,
       case when p.started then coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0)
           - coalesce(p.delivery_subsidy, 0) - coalesce(p.carrier_returns, 0) - coalesce(p.returns, 0)
           - coalesce(p.acquiring, 0) end as margin_before_cac,
       case when p.started then coalesce(p.ads, 0) end as ads,
       case when p.started then coalesce(p.ads_bonus, 0) end as ads_bonus,
       case when p.started then p.contractor end as contractor,
       case when p.started then p.other_opex end as other_opex,
       case when p.started then p.claude_api end as claude_api,
       case when p.started then p.usn_reserve end as usn_reserve,
       case when p.started and p.month_no > 0 then
           case extract(year from p.period_start) when 2026 then fin.inp('ip_fixed_contrib_2026')
                                                  when 2027 then fin.inp('ip_fixed_contrib_2027') end / 12 end as contributions_reserve,
       case when p.started then
           coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0) - coalesce(p.delivery_subsidy, 0)
           - coalesce(p.carrier_returns, 0) - coalesce(p.returns, 0) - coalesce(p.acquiring, 0) - coalesce(p.ads, 0)
           - p.contractor - p.other_opex - coalesce(p.usn_reserve, 0)
       end as operating_profit,
       p.revenue / nullif(p.orders, 0) as avg_check,
       (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as cac,
       p.first_orders_margin_before_usn / nullif(p.first_orders, 0)
           - (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as first_order_after_cac,
       case when p.started then coalesce(p.usn_income, 0) end as usn_income,
       case when p.started then coalesce(p.contributions_paid, 0) end as contributions_paid
from p;

-- --- цели и лимиты KPI --------------------------------------------------------------------
create or replace view fin.v_kpi_targets as
select 'cac' as metric, fin.inp('kpi_cac_goal') as goal, fin.inp('kpi_cac_limit') as limit_value
union all select 'avg_check', fin.inp('kpi_avg_check_goal'), null;
comment on view fin.v_kpi_targets is 'Цели и лимиты KPI из вводных kpi_*: CAC (цель и лимит), средний чек (цель).';

-- --- сверка выплат ЮKassa с выпиской (реестр бота) -------------------------------------
-- finance.yk_payouts ведёт бот (сверка реестра); дашборды читают его через fin.
create or replace view fin.v_yk_payouts as
select payout_date, bank_amount, days_from, days_to, registry_amount, diff, status, checked_at
from finance.yk_payouts;
comment on view fin.v_yk_payouts is 'Выплаты ЮKassa из выписки против реестра платежей: какие дни вошли, расхождение.';

-- Оплаты, чек по которым ЮKassa не зарегистрировала за час.
create or replace view fin.v_yk_receipts_missing as
select count(*) as payments, max(captured_at) as last_captured
from finance.yk_payments
where status = 'succeeded' and captured_at < now() - interval '1 hour'
  and coalesce(receipt_registration, '') <> 'succeeded'
having count(*) > 0;

-- --- «Требует внимания» -----------------------------------------------------------------
-- Одна строка — одна проблема: уровень (🔴 / 🟡), текст, куда идти исправлять
-- (place — ключ вкладки, ссылку подставляет дашборд). Нет проблем — «Всё в порядке».
create or replace view fin.v_attention as
with ps as (select * from fin.v_pilot_status),
money as (select * from fin.v_money_now),
cap as (select * from fin.v_capital_status),
budget as (
    select coalesce(sum(greatest(coalesce(left_amount, 0), 0)), 0) as left_before_start
    from fin.v_pilot_budget_status where kind in ('разовая', 'реклама')
),
checks as (
    -- Вклады не покрывают оставшийся бюджет до старта.
    select 1 as sort,
           case when m.free_cash + c.remaining < b.left_before_start then '🔴' else '🟡' end as level,
           'Денег не хватает на бюджет до старта: осталось потратить ' || fin.fmt_rub(b.left_before_start)
               || ', свободно ' || fin.fmt_rub(m.free_cash)
               || case when c.remaining > 0 then ', ждём вкладов ' || fin.fmt_rub(c.remaining) else '' end as text,
           'money' as place
    from money m cross join cap c cross join budget b cross join ps
    where not ps.started and m.free_cash < b.left_before_start
    union all
    select 2, '🟡', 'Не разнесены покупки с карты: переведено себе на покупки ' || fin.fmt_rub(r.transferred)
               || ', внесено «мимо счёта» ' || fin.fmt_rub(r.recorded) || ', разница ' || fin.fmt_rub(r.unreconciled),
           'entry'
    from fin.v_card_reconcile_status r where r.overdue
    union all
    select 3, case when ps.days_to_start < 7 then '🔴' else '🟡' end,
           'Партия чая не внесена, до старта ' || ps.days_to_start || ' дн.', 'entry'
    from ps where not ps.started and ps.days_to_start < coalesce(fin.inp('tea_lot_warn_days')::int, 21)
      and not exists (select 1 from fin.purchase_lots)
    union all
    select 4, '🟡', 'Не разобрано операций выписки: ' || count(*) || ' на ' || fin.fmt_rub(sum(amount)), 'checks'
    from fin.v_bank_ops where article_id = 'tech_unclassified' having count(*) > 0
    union all
    select 5, '🟡', 'Снимок остатка ' || to_char(c.snapshot_date, 'DD.MM.YYYY') || ' расходится с банком на '
               || fin.fmt_rub(c.snapshot_diff), 'checks'
    from fin.v_cash_status c where coalesce(c.snapshot_diff, 0) <> 0
    union all
    select 6, '🟡', 'Claude: не учтено ботом ' || fin.fmt_pct(s.unaccounted_share) || ' счёта Anthropic за 7 дней', 'checks'
    from fin.v_claude_status s where s.unaccounted_share > coalesce(fin.inp('claude_unaccounted_max'), 0.20)
    union all
    -- После старта: оплаченные заказы, которые ещё не у перевозчика дольше суток.
    select 7, '🔴', 'Оплачены и не переданы перевозчику дольше суток: ' || count(*) || ' заказ(ов)', 'sales'
    from analytics.v_orders o cross join ps
    where ps.started and o.is_paid and not o.is_refunded and not o.is_canceled and o.paid_date_msk < current_date
      and not exists (select 1 from analytics.v_funnel_events e where e.order_id = o.order_id
                      and e.event in ('handed_over', 'at_pickup_point', 'delivered', 'not_delivered'))
    having count(*) > 0
    union all
    -- Открытые вопросы менеджеру: последнее событие клиента — «открыт».
    select 8, '🟡', 'Вопросы менеджеру без ответа: ' || count(*), 'sales'
    from (
        select distinct on (client_key) client_key, event from analytics.v_funnel_events
        where event in ('escalation_opened', 'escalation_closed') and client_key is not null
        order by client_key, created_at desc
    ) e cross join ps
    where ps.started and e.event = 'escalation_opened'
    having count(*) > 0
    union all
    select 9, '🔴', 'Чек не зарегистрирован у ЮKassa: ' || r.payments || ' оплат(ы)', 'checks'
    from fin.v_yk_receipts_missing r
    union all
    select 10, '🟡', 'Выплаты ЮKassa не сходятся с реестром: ' || count(*) || ' на ' || fin.fmt_rub(sum(abs(diff))), 'checks'
    from fin.v_yk_payouts where status = 'mismatch' having count(*) > 0
)
select sort, level, text, place from checks
union all
select 0, '🟢', 'Всё в порядке', null where not exists (select 1 from checks)
order by 1;
comment on view fin.v_attention is
    'Требует внимания: вклады против бюджета до старта, не разнесённые покупки с карты, партия чая, «Не разобрано», сверка остатка, Claude, чеки и выплаты ЮKassa, после старта — заказы и вопросы менеджеру.';

insert into fin.schema_migrations (version) values ('009_dashboards') on conflict do nothing;
