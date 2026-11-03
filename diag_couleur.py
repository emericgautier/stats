import re, sys
from bs4 import BeautifulSoup
import suivi
URLS = ["https://www.vinted.fr/items/10632389030-sezane-blazer",
        "https://www.vinted.fr/items/10633206889-polaire-patagonia-sherpa-noir-m",
        "https://www.vinted.fr/items/10631756478-vitow-cardigan-american-vintage-flieder"]
suivi.BASE = "https://www.vinted.fr"
suivi.ouvrir_session()
for u in URLS:
    r = suivi.session.get(u, headers={"Accept": "text/html", "Referer": suivi.BASE + "/"}, timeout=25)
    print("=====", u, r.status_code, len(r.text))
    soup = BeautifulSoup(r.text, "html.parser")
    for el in soup.select('[data-testid*="color" i], [data-testid*="colour" i], [itemprop="color"]'):
        print("TESTID", el.get("data-testid"), el.name, "|", el.get_text(" ", strip=True)[:120])
    for el in soup.find_all(string=re.compile(r"Couleur", re.I))[:5]:
        p = el.parent
        print("TEXTE", p.name, p.get("data-testid"), p.get("class"), "| parent:", p.parent.get("data-testid") if p.parent else None, "|", (p.parent.parent.get_text(" | ", strip=True)[:200] if p.parent and p.parent.parent else ""))
    for m in list(re.finditer(r'.{0,80}(colou?r[_a-z0-9]*)\\?"\s*:.{0,120}', r.text, re.I))[:12]:
        print("JSON", m.group(0).replace("\n", " ")[:220])
    for el in soup.select('[data-testid^="item-attributes"], [data-testid^="item-details"]')[:30]:
        print("ATTR", el.get("data-testid"), "|", el.get_text(" ", strip=True)[:100])
