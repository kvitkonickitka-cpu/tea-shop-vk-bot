"""Пробный прогон сборки Metabase: живой Metabase только читается, сравнивается задуманное с живым.

    MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" python3 mb_dryrun.py            # отчёт
    MB_FIN_PW=… python3 mb_dryrun.py --json plan.json                                      # и задуманное в файл

Сборка (dash_build.py) запускается как есть, но вызовы API подменены: чтения (GET и
запросы /dataset) идут в настоящий Metabase, записи только запоминаются. Вопросы и
действия пересоздаются при каждом прогоне с новыми id, поэтому сравнение — по вкладке
и месту карточки на сетке, по названию, запросу и настройкам, а не по id.

Пусто в отчёте — прогон dash_build.py ничего видимого не изменит. Так проверяется,
что ручные правки в Metabase перенесены в скрипт и следующий прогон их не откатит.
"""
import json
import re
import runpy
import sys

import mbapi

real = mbapi.call
seq = [10 ** 9]
CARDS, ACTIONS, DASH = {}, {}, {}
LIVE_DASH = {}


def _id():
    seq[0] += 1
    return seq[0]


def _dashboard(did):
    if did not in DASH:
        live = real("GET", f"/dashboard/{did}") if did < 10 ** 9 else {"tabs": [], "dashcards": [], "parameters": []}
        LIVE_DASH[did] = live
        DASH[did] = {"id": did, "name": live.get("name"), "description": live.get("description"),
                     "tabs": live.get("tabs", []), "dashcards": None, "parameters": live.get("parameters", []),
                     "collection_position": live.get("collection_position")}
    return DASH[did]


def fake(method, path, body=None):
    m = re.fullmatch(r"/dashboard/(\d+)", path)
    if method == "GET":
        if m:
            d = _dashboard(int(m.group(1)))
            return {**LIVE_DASH[d["id"]], **{k: v for k, v in d.items() if v is not None}}
        return real(method, path)
    if method == "POST" and path == "/dataset":
        return real(method, path, body)
    if method == "POST" and re.fullmatch(r"/card/\d+/query", path):
        return {"status": "completed"}
    if method == "POST" and path == "/card":
        cid = _id()
        CARDS[cid] = body
        return {"id": cid}
    if method == "POST" and path == "/action":
        aid = _id()
        ACTIONS[aid] = body
        return {"id": aid}
    if method == "POST" and path == "/dashboard":
        did = _id()
        _dashboard(did)["name"] = body.get("name")
        return {"id": did}
    if method == "POST":
        return {"id": _id(), **(body or {})}
    if method == "PUT" and m:
        d = _dashboard(int(m.group(1)))
        body = dict(body or {})
        if "tabs" in body:
            d["tabs"] = [{**t, "id": t["id"] if t["id"] > 0 else _id()} for t in body["tabs"]]
            # Вкладки с отрицательным id — новые: карточки ссылаются на них уже по новым id.
        for key in ("dashcards", "parameters", "description", "name", "collection_position", "archived"):
            if key in body:
                d[key] = body[key]
        return {}
    return {}


def _plan_cards(d):
    tabs = {t["id"]: t["name"] for t in d["tabs"]}
    out = []
    for dc in d["dashcards"] or []:
        viz = dc.get("visualization_settings") or {}
        item = {"tab": tabs.get(dc.get("dashboard_tab_id"), dc.get("dashboard_tab_id")), "row": dc["row"], "col": dc["col"],
                "w": dc["size_x"], "h": dc["size_y"], "dc_viz": {k: v for k, v in viz.items() if k != "virtual_card"}}
        if dc.get("card_id"):
            c = CARDS.get(dc["card_id"], {})
            item.update(kind="card", name=c.get("name"), display=c.get("display"), description=c.get("description"),
                        query=c.get("dataset_query", {}).get("native", {}).get("query"),
                        card_viz=c.get("visualization_settings"))
        elif dc.get("action_id"):
            item.update(kind="button", name=viz.get("button.label"))
        else:
            item.update(kind="text", name=(viz.get("text") or "")[:60])
        out.append(item)
    return out


def _live_cards(did):
    live = real("GET", f"/dashboard/{did}")
    tabs = {t["id"]: t["name"] for t in live.get("tabs", [])}
    out = []
    for dc in live["dashcards"]:
        viz = dc.get("visualization_settings") or {}
        item = {"tab": tabs.get(dc.get("dashboard_tab_id"), dc.get("dashboard_tab_id")), "row": dc["row"], "col": dc["col"],
                "w": dc["size_x"], "h": dc["size_y"], "dc_viz": {k: v for k, v in viz.items() if k != "virtual_card"}}
        c = dc.get("card") or {}
        if dc.get("card_id"):
            item.update(kind="card", name=c.get("name"), display=c.get("display"), description=c.get("description"),
                        query=_native(c.get("dataset_query")),
                        card_viz=c.get("visualization_settings"))
        elif dc.get("action_id"):
            item.update(kind="button", name=viz.get("button.label"))
        else:
            item.update(kind="text", name=(viz.get("text") or "")[:60])
        out.append(item)
    return live, out


def _native(dq):
    """Текст запроса: Metabase 0.64 отдаёт его в формате stages, а сборка шлёт native."""
    dq = dq or {}
    if "native" in dq:
        return dq["native"].get("query")
    for stage in dq.get("stages") or []:
        if stage.get("native"):
            return stage["native"]
    return None


def _norm_viz(viz):
    # Metabase дописывает к настройкам свои служебные ключи — сравниваем только заданные сборкой.
    return json.dumps(viz or {}, ensure_ascii=False, sort_keys=True)


def compare(did, plan):
    live, have = _live_cards(did)
    diffs = []
    for key in ("name", "description"):
        if (plan.get(key) or None) != (live.get(key) or None) and plan.get(key) is not None:
            diffs.append(f"дашборд {key}: живой «{live.get(key)}» → сборка «{plan.get(key)}»")
    if [t["name"] for t in plan["tabs"]] != [t["name"] for t in live.get("tabs", [])]:
        diffs.append(f"вкладки: живые {[t['name'] for t in live.get('tabs', [])]} → сборка {[t['name'] for t in plan['tabs']]}")
    pp = {p["id"]: {k: v for k, v in p.items()} for p in plan.get("parameters") or []}
    lp = {p["id"]: {k: v for k, v in p.items()} for p in live.get("parameters") or []}
    if json.dumps(pp, sort_keys=True, ensure_ascii=False) != json.dumps(lp, sort_keys=True, ensure_ascii=False):
        diffs.append(f"фильтры: живые {json.dumps(lp, ensure_ascii=False)} → сборка {json.dumps(pp, ensure_ascii=False)}")
    at = lambda items: {(i["tab"], i["row"], i["col"]): i for i in items}  # noqa: E731
    want, got = at(_plan_cards(plan)), at(have)
    for pos in sorted(set(want) | set(got), key=lambda p: (str(p[0]), p[1], p[2])):
        w, g = want.get(pos), got.get(pos)
        where = f"{pos[0]} r{pos[1]} c{pos[2]}"
        if w is None:
            diffs.append(f"{where}: в живом есть «{g['name']}» ({g['kind']}), сборка её не ставит")
            continue
        if g is None:
            diffs.append(f"{where}: сборка ставит «{w['name']}» ({w['kind']}), в живом нет")
            continue
        for key in ("kind", "name", "w", "h", "display", "description", "query"):
            if (w.get(key) or None) != (g.get(key) or None):
                diffs.append(f"{where} «{g.get('name')}»: {key}: живой {json.dumps(g.get(key), ensure_ascii=False)[:400]}"
                             f" → сборка {json.dumps(w.get(key), ensure_ascii=False)[:400]}")
        if w["kind"] == "card":
            for key in sorted(set(w.get("card_viz") or {}) | set(g.get("card_viz") or {})):
                a, b = (g.get("card_viz") or {}).get(key), (w.get("card_viz") or {}).get(key)
                if _norm_viz(a) != _norm_viz(b) and b is not None:
                    diffs.append(f"{where} «{g.get('name')}»: настройка {key}: живой {_norm_viz(a)[:300]} → сборка {_norm_viz(b)[:300]}")
        if _norm_viz(w["dc_viz"]) != _norm_viz(g["dc_viz"]):
            diffs.append(f"{where} «{g.get('name')}»: на дашборде: живой {_norm_viz(g['dc_viz'])[:300]} → сборка {_norm_viz(w['dc_viz'])[:300]}")
    return diffs


if __name__ == "__main__":
    mbapi.call = fake
    target = "dash_build"
    try:
        runpy.run_module(target, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(f"сборка завершилась с кодом {exc.code}")
    total = 0
    for did, plan in DASH.items():
        if plan["dashcards"] is None or plan.get("archived"):
            continue
        if did >= 10 ** 9:
            print(f"== новый дашборд «{plan['name']}»: {len(plan['dashcards'])} карточек")
            continue
        diffs = compare(did, plan)
        total += len(diffs)
        print(f"== /dashboard/{did} «{plan['name']}»: {'совпадает' if not diffs else f'отличий {len(diffs)}'}")
        for line in diffs:
            print("  -", line)
    if "--json" in sys.argv:
        json.dump({did: {**p, "cards": _plan_cards(p)} for did, p in DASH.items() if p["dashcards"] is not None},
                  open(sys.argv[sys.argv.index("--json") + 1], "w"), ensure_ascii=False, indent=1, default=str)
    raise SystemExit(1 if total else 0)
