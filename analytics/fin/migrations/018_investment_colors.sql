-- 018: свои цвета у вложений. Все три были одного синего (#3465A4) и сливались в
-- стопке «Куда ушли деньги по месяцам»; владелец 24.10.2026 развёл их вручную на
-- карточке — здесь те же цвета в справочнике, чтобы их брали все графики и сборка.

update fin.budget_articles set color = '#1A5FB4' where item = 'Термопринтер';
update fin.budget_articles set color = '#99C1F1' where item = 'Весы';
update fin.budget_articles set color = '#613583' where item = 'ККТ, ФН, фискализация';

insert into fin.schema_migrations (version) values ('018_investment_colors') on conflict do nothing;
