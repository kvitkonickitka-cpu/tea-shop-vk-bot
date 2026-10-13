from mbapi import call
for did in (2, 3):
    d = call("GET", f"/dashboard/{did}")
    tabs = {t["id"]: t["name"] for t in d.get("tabs", [])}
    by_tab = {}
    unmapped = []
    for c in d["dashcards"]:
        by_tab.setdefault(tabs.get(c.get("dashboard_tab_id"), "—"), 0)
        by_tab[tabs.get(c.get("dashboard_tab_id"), "—")] += 1
        if not c["parameter_mappings"]:
            unmapped.append(c["card"]["name"])
    print(d["name"], "| фильтры:", [p["name"] for p in d["parameters"]], "| карточек по вкладкам:", by_tab, "| без фильтра:", unmapped)
