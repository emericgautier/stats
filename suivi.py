"""Suivi des ventes par recherche, avec notification et rapport HTML.

Chaque passage :
  1. lit les recherches du secret RECHERCHES (une URL de recherche par ligne) ;
  2. ajoute au suivi les nouvelles annonces de chaque recherche ;
  3. revérifie les annonces suivies qui ont disparu des résultats ;
  4. envoie une notification ntfy pour chaque vente rapide ;
  5. régénère docs/index.html.

Aucune connexion à un compte : seulement des pages publiques.
"""

import html
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_RECHERCHES = os.path.join(ICI, "recherches.txt")  # repli local, hors dépôt
FICHIER_PRIX_MIN = os.path.join(ICI, "prix_min.txt")  # prix minimum par recherche, modifiable sur GitHub
FICHIER_ETAT = os.path.join(ICI, "etat.json")
FICHIER_RAPPORT = os.path.join(ICI, "docs", "index.html")

BASE = ""  # déduit de la première recherche
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# Réglages
FENETRE_SUIVI_H = 72        # durée de suivi d'une annonce après sa publication
NOTIF_MAX_H = 24            # notifier seulement les ventes parties en moins de X heures
PAGES_PAR_RECHERCHE = 1     # 96 annonces les plus récentes, largement assez toutes les 30 min
MAX_VERIFS_PAR_PASSAGE = 80 # pages d'annonces ouvertes au maximum par passage
PAUSE = (2.0, 4.0)          # pause entre deux pages d'annonces (secondes)
CONSERVATION_J = 10         # oubli des annonces plus anciennes

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

session = requests.Session()
session.headers.update({"User-Agent": random.choice(UA), "Accept-Language": "fr-FR,fr;q=0.9"})


def maintenant():
    return time.time()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- site

def ouvrir_session():
    r = session.get(BASE + "/", headers={"Accept": "text/html"}, timeout=25)
    log(f"Page d'accueil : HTTP {r.status_code}, cookies : {', '.join(session.cookies.keys()) or 'aucun'}")
    return r.status_code == 200


def params_depuis_url(url):
    """Transforme une URL de recherche en paramètres de l'API catalogue."""
    p = {}
    for k, v in parse_qsl(urlsplit(url).query):
        k = k.replace("[]", "")
        if k == "catalog":
            k = "catalog_ids"
        if k in ("time", "search_id", "page", "per_page", "order", "disabled_personalization"):
            continue
        p.setdefault(k, []).append(v)
    p = {k: ",".join(v) for k, v in p.items()}
    p.update({"order": "newest_first", "per_page": "96"})
    return p


def url_page(url, page):
    """Même recherche, triée des plus récentes, à la page voulue."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in ("page", "order", "time", "search_id")]
    q += [("order", "newest_first"), ("page", str(page))]
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def ts_photo(url):
    """Les photos sont souvent nommées d'après leur heure d'envoi (horodatage Unix)."""
    m = re.search(r"/(\d{10})\.(?:jpe?g|webp|png)", url or "")
    if m and 1.5e9 < int(m.group(1)) < 2.2e9:
        return float(m.group(1))
    return None


def items_depuis_flux(texte):
    """Annonces présentes dans les données embarquées de la page (format self.__next_f)."""
    morceaux = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', texte):
        try:
            morceaux.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    flux = "".join(morceaux)
    dec = json.JSONDecoder()
    vus, out = set(), []
    for m in re.finditer(r'\{"id":\s*\d{6,}', flux):
        try:
            obj, _ = dec.raw_decode(flux, m.start())
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj["id"] in vus:
            continue
        if "title" in obj and ("price" in obj or "photo" in obj):
            vus.add(obj["id"])
            out.append(obj)
    return out


def items_depuis_grille(texte):
    """Annonces lues dans la grille HTML, si les données embarquées manquent."""
    soup = BeautifulSoup(texte, "html.parser")
    out, vus = [], set()
    for bloc in soup.select('[data-testid^="product-item-id-"]'):
        m = re.search(r"product-item-id-(\d+)", bloc.get("data-testid", ""))
        if not m or m.group(1) in vus:
            continue
        i = m.group(1)
        vus.add(i)
        a = bloc.select_one('a[href*="/items/"]')
        info = (a.get("title") if a else "") or ""
        img = bloc.select_one("img")
        src = (img.get("src") or img.get("data-src") or "") if img else ""
        champ = lambda nom: (re.search(nom + r"\s*:\s*([^,]+)", info) or [None, ""])[1].strip()
        pm = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*€", info) or re.search(r"(\d+(?:[.,]\d{1,2})?)\s*€", bloc.get_text(" "))
        titre = info.split(", marque")[0] if ", marque" in info else ((img.get("alt") if img else "") or info)
        href = a.get("href", "") if a else ""
        out.append({
            "id": int(i),
            "title": titre.split(", ")[0] if titre == info else titre,
            "price": {"amount": pm.group(1).replace(",", ".") if pm else None},
            "brand_title": champ("marque"),
            "size_title": champ("taille"),
            "status": champ("état"),
            "photo": {"url": src},
            "url": href if href.startswith("http") else BASE + href if href else f"{BASE}/items/{i}",
            "favourite_count": None,
        })
    return out


DERNIERE_METHODE = ""
EXEMPLE_BRUT = ""


def _titre(v):
    if isinstance(v, dict):
        return v.get("title") or v.get("name") or ""
    return v or ""


def normaliser(o):
    """Ramène les différents formats de données du site à un seul."""
    o = dict(o)
    photos = o.get("photos") or []
    p0 = photos[0] if photos and isinstance(photos[0], dict) else {}
    ph = o.get("photo") if isinstance(o.get("photo"), dict) else {}
    url_photo = ph.get("url") or o.get("thumbnailUrl") or p0.get("url") or ""
    hr = ph.get("high_resolution") or ph.get("highResolution") or p0.get("high_resolution") or p0.get("highResolution") or {}
    o["photo"] = {"url": url_photo, "high_resolution": {"timestamp": hr.get("timestamp")}}
    box = o.get("itemBox") if isinstance(o.get("itemBox"), dict) else {}
    ligne2 = [x.strip() for x in str(box.get("secondLine") or "").split("·") if x.strip()]
    o["brand_title"] = o.get("brand_title") or o.get("brandTitle") or _titre(o.get("brand")) or str(box.get("firstLine") or "")
    o["size_title"] = o.get("size_title") or o.get("sizeTitle") or _titre(o.get("size")) or (ligne2[0] if len(ligne2) > 1 else "")
    o["status"] = o.get("status") if isinstance(o.get("status"), str) and o.get("status") else (o.get("statusTitle") or _titre(o.get("condition")) or (ligne2[-1] if ligne2 else ""))
    if o.get("favourite_count") is None:
        o["favourite_count"] = o.get("favouriteCount")
    return o


def fusionner(flux, grille):
    """Complète les annonces de la grille avec les données embarquées (et inversement)."""
    global DERNIERE_METHODE, EXEMPLE_BRUT
    if flux:
        EXEMPLE_BRUT = "Clés : " + ", ".join(flux[0].keys()) + "\n" + json.dumps({k: v for k, v in flux[0].items() if k not in ("photos", "thumbnailUrls", "user", "serviceFee", "totalItemPrice")}, ensure_ascii=False)[:2500]
    par_id = {str(o["id"]): o for o in flux}
    out = []
    for g in grille:
        f = par_id.pop(str(g["id"]), {})
        m = dict(g)
        for k, v in f.items():
            if k not in m or m[k] in (None, "", {}) or (k == "price" and not (m[k] or {}).get("amount")):
                m[k] = v
            elif k == "photo" and isinstance(v, dict):
                m["photo"] = {**v, **{kk: vv for kk, vv in m["photo"].items() if vv}}
        if m.get("brand") and not m.get("brand_title"):
            m["brand_title"] = m["brand"] if isinstance(m["brand"], str) else (m["brand"] or {}).get("title", "")
        out.append(m)
    out += list(par_id.values())
    out = [normaliser(o) for o in out]
    DERNIERE_METHODE = f"grille {len(grille)} · données {len(flux)} · total {len(out)}"
    return out


def chercher(url, page=1):
    global DERNIERE_METHODE
    for essai in range(2):
        r = session.get(url_page(url, page), headers={"Accept": "text/html", "Referer": BASE + "/"}, timeout=30)
        if r.status_code == 200:
            return fusionner(items_depuis_flux(r.text), items_depuis_grille(r.text))
        if r.status_code in (401, 403) and essai == 0:
            log(f"   Recherche refusée (HTTP {r.status_code}), nouvelle session…")
            session.cookies.clear()
            ouvrir_session()
            continue
        if r.status_code == 429:
            log("   Trop de requêtes, pause de 60 s")
            time.sleep(60)
            continue
        raise RuntimeError(f"Recherche : HTTP {r.status_code}")
    return []


def prix(it):
    p = it.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def publie_le(it):
    ph = it.get("photo") or {}
    ts = (ph.get("high_resolution") or {}).get("timestamp")
    return float(ts) if ts else ts_photo(ph.get("url"))


def annonce_depuis_api(it, nom_recherche):
    ph = it.get("photo") or {}
    return {
        "id": str(it["id"]),
        "recherche": nom_recherche,
        "titre": it.get("title") or "",
        "prix": prix(it),
        "marque": it.get("brand_title") or "",
        "taille": it.get("size_title") or "",
        "etat_article": it.get("status") or "",
        "photo": ph.get("url") or "",
        "url": it.get("url") or f"{BASE}/items/{it['id']}",
        "likes": it.get("favourite_count"),
        "publie": publie_le(it),
        "vu": maintenant(),
        "dernier_dispo": maintenant(),
        "statut": "dispo",
        "vendu_vu": None,
    }


def statut_page(url):
    """Ouvre la page publique d'une annonce : vendu, réservé, dispo ou supprimé."""
    r = session.get(url, headers={"Accept": "text/html", "Referer": BASE + "/"}, timeout=25, allow_redirects=True)
    if r.status_code in (404, 410):
        return "supprime"
    if r.status_code == 429:
        time.sleep(60)
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.select_one('.web_ui__Cell__success, [class*="Cell__success"], [class*="cell--success"], [class*="item-status--sold"]'):
        return "vendu"
    tag = soup.select_one('[data-testid="item-status--content"]')
    if tag:
        t = tag.get_text(strip=True).lower()
        if "vendu" in t:
            return "vendu"
        if "réserv" in t or "reserv" in t:
            return "reserve"
    if soup.select_one('[class*="Cell__warning"], [class*="cell--warning"]'):
        return "reserve"
    if re.search(r'"is_closed"\s*:\s*true', r.text) and re.search(r'"is_reserved"\s*:\s*false', r.text):
        return "vendu"
    return "dispo"


# ---------------------------------------------------------------- notifications

def notifier(titre, message, lien=None, photo=None, tags="moneybag"):
    if not NTFY_TOPIC:
        log(f"   (pas de NTFY_TOPIC) {titre} — {message}")
        return
    h = {"Title": titre.encode("utf-8"), "Tags": tags}
    if lien:
        h["Click"] = lien
    if photo:
        h["Attach"] = photo
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=h, timeout=15)
    except requests.RequestException as e:
        log(f"   Notification non envoyée : {e}")


# ---------------------------------------------------------------- état

def lire_recherches():
    global BASE
    out = []
    texte = os.environ.get("RECHERCHES", "")
    if not texte.strip() and os.path.exists(FICHIER_RECHERCHES):
        with open(FICHIER_RECHERCHES, encoding="utf-8") as f:
            texte = f.read()
    if True:
        for ligne in texte.splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            if "|" in ligne:
                nom, url = [x.strip() for x in ligne.split("|", 1)]
            else:
                nom, url = ligne, ligne
            out.append((nom, url))
    prix_min = lire_prix_min()
    out = [(nom, avec_prix_min(url, prix_min[nom]) if nom in prix_min else url) for nom, url in out]
    if out and not BASE:
        u = urlsplit(out[0][1])
        BASE = f"{u.scheme}://{u.netloc}"
    return out


def lire_prix_min():
    """Prix minimum par nom de recherche (lignes « Nom | prix »)."""
    out = {}
    if os.path.exists(FICHIER_PRIX_MIN):
        with open(FICHIER_PRIX_MIN, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "|" not in ligne:
                    continue
                nom, p = [x.strip() for x in ligne.rsplit("|", 1)]
                p = p.replace("€", "").replace(",", ".").strip()
                if nom and re.fullmatch(r"\d+(?:\.\d+)?", p):
                    out[nom] = p
    return out


def avec_prix_min(url, p):
    """Remplace le prix minimum de l'adresse de recherche."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k != "price_from"] + [("price_from", p)]
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def lire_etat():
    try:
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"annonces": {}}


def ecrire_etat(etat):
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, separators=(",", ":"))


def heures(a, b):
    return (b - a) / 3600 if a and b else None


def fmt_h(h):
    if h is None:
        return "?"
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.0f} j"


def fmt_prix(p):
    if p is None:
        return "?"
    return f"{p:.0f} €" if p == int(p) else f"{p:.2f} €".replace(".", ",")


# ---------------------------------------------------------------- passage

def passage():
    etat = lire_etat()
    ann = etat["annonces"]
    t0 = maintenant()
    recherches = lire_recherches()
    if not recherches:
        log("Aucune recherche : ajoute le secret RECHERCHES.")
        return
    ouvrir_session()

    presents = set()
    deja_vues = set(etat.get("recherches_vues", []))
    for nom, url in recherches:
        nouveaux = 0
        try:
            for page in range(1, PAGES_PAR_RECHERCHE + 1):
                items = chercher(url, page)
                for it in items:
                    i = str(it["id"])
                    presents.add(i)
                    if i in ann:
                        a = ann[i]
                        a["dernier_dispo"] = t0
                        a["likes"] = it.get("favourite_count", a.get("likes"))
                        if a["statut"] == "reserve":
                            a["statut"] = "dispo"
                    else:
                        pub = publie_le(it)
                        if pub and t0 - pub > FENETRE_SUIVI_H * 3600:
                            continue
                        a = annonce_depuis_api(it, nom)
                        if not a["publie"]:
                            # Recherche déjà connue : l'annonce vient d'apparaître (à 30 min près).
                            # Premier passage : âge inconnu, l'annonce est suivie mais exclue des délais.
                            a["publie"] = t0 if nom in deja_vues else None
                        ann[i] = a
                        nouveaux += 1
                if len(items) < 20:
                    break
                time.sleep(random.uniform(*PAUSE))
        except Exception as e:
            log(f"[{nom}] erreur de recherche : {e}")
            continue
        log(f"[{nom}] {nouveaux} nouvelles annonces")
        etat.setdefault("recherches_vues", [])
        if nom not in etat["recherches_vues"]:
            etat["recherches_vues"].append(nom)

    # Annonces à revérifier : disparues des résultats, encore dans la fenêtre de suivi
    a_verifier = [a for i, a in ann.items()
                  if a["statut"] in ("dispo", "reserve") and i not in presents
                  and t0 - (a.get("publie") or a["vu"]) < FENETRE_SUIVI_H * 3600]
    a_verifier.sort(key=lambda a: a.get("publie") or a["vu"], reverse=True)
    a_verifier = a_verifier[:MAX_VERIFS_PAR_PASSAGE]
    log(f"{len(a_verifier)} annonces à revérifier")

    ventes = []
    for a in a_verifier:
        try:
            s = statut_page(a["url"])
        except requests.RequestException as e:
            log(f"   {a['id']} : {e}")
            s = None
        if s == "vendu":
            a["statut"] = "vendu"
            a["vendu_vu"] = t0
            ventes.append(a)
        elif s == "supprime":
            a["statut"] = "supprime"
        elif s == "reserve":
            a["statut"] = "reserve"
        elif s == "dispo":
            a["dernier_dispo"] = t0
        time.sleep(random.uniform(*PAUSE))

    # Notifications : seulement les ventes rapides
    rapides = [a for a in ventes if (heures(a.get("publie"), a["vendu_vu"]) or 1e9) <= NOTIF_MAX_H]
    log(f"{len(ventes)} ventes détectées, dont {len(rapides)} en moins de {NOTIF_MAX_H} h")
    if est_nuit():
        # La nuit : pas de notification, les ventes rapides sont gardées pour le résumé de 7 h
        etat.setdefault("nuit", []).extend(a["id"] for a in rapides)
        rapides = []
    elif etat.get("nuit"):
        nuit = [ann[i] for i in etat.pop("nuit") if i in ann]
        if nuit:
            nuit.sort(key=lambda a: heures(a.get("publie"), a["vendu_vu"]) or 1e9)
            lignes = [f"{fmt_prix(a['prix'])} · {fmt_h(heures(a.get('publie'), a['vendu_vu']))} · {a['recherche']} · {a['titre']}" for a in nuit[:10]]
            notifier(f"Cette nuit : {len(nuit)} vente{'s' if len(nuit) > 1 else ''} rapide{'s' if len(nuit) > 1 else ''}",
                     "\n".join(lignes), lien=nuit[0]["url"], photo=nuit[0]["photo"], tags="sunrise")
    if len(rapides) > 5:
        lignes = [f"{fmt_prix(a['prix'])} · {fmt_h(heures(a.get('publie'), a['vendu_vu']))} · {a['recherche']} · {a['titre']}" for a in rapides[:8]]
        notifier(f"{len(rapides)} ventes rapides", "\n".join(lignes), tags="fire")
    else:
        for a in rapides:
            delai = fmt_h(heures(a.get("publie"), a["vendu_vu"]))
            details = " · ".join(x for x in (a["marque"], a["taille"], a["etat_article"]) if x)
            notifier(f"{fmt_prix(a['prix'])} · VENDU en {delai} · {a['recherche']}",
                     a["titre"] + (f"\n{details}" if details else ""),
                     lien=a["url"], photo=a["photo"])

    # Ménage
    limite = t0 - CONSERVATION_J * 86400
    for i in [i for i, a in ann.items() if (a.get("publie") or a["vu"]) < limite]:
        del ann[i]

    etat["dernier_passage"] = t0
    ecrire_etat(etat)
    ecrire_rapport(etat)


# ---------------------------------------------------------------- rapport

CSS = """:root{--bg:#f6f6f3;--surface:#fff;--ink:#1d2120;--muted:#646b68;--line:#dfe1dc;--accent:#2f5d50;--accent-soft:#e3ece8;--hot:#9a3b12;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141716;--surface:#1d2120;--ink:#ecefec;--muted:#9aa29e;--line:#2e3431;--accent:#8cc2ad;--accent-soft:#22312b;--hot:#e3a06a;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif;padding:max(28px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 48px max(16px,env(safe-area-inset-left))}
.wrap{max-width:1200px;margin:0 auto;display:flex;flex-direction:column;gap:24px}h1{font-size:28px;margin:0}h2{font-size:20px;margin:0 0 8px}
.sub{color:var(--muted);font-size:14px;margin:4px 0 0}.tbl{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}td:not(:first-child){font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}
.ph{position:relative;aspect-ratio:3/4;background:var(--accent-soft);display:grid;place-items:center;color:var(--muted);font-size:13px}.ph img{width:100%;height:100%;object-fit:cover}
.b{position:absolute;padding:2px 7px;border-radius:4px;font-size:12px;font-weight:600}.p{bottom:8px;left:8px;background:var(--ink);color:var(--bg)}.d{top:8px;right:8px;background:var(--surface)}.d.hot{color:var(--hot)}
.body{padding:8px 10px 10px;display:flex;flex-direction:column;gap:3px;flex:1;font-size:13px}.t{font-weight:600}.m{color:var(--muted);font-size:12px}
.body a{margin-top:auto;color:var(--accent);font-weight:600;text-decoration:none}
.chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;margin-bottom:12px}.chips button{flex:none;font:600 13px system-ui;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:7px 14px}
.chips button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--bg)}[hidden]{display:none!important}
@media (max-width:520px){.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:24px}}"""


def ecrire_rapport(etat):
    ann = list(etat["annonces"].values())
    t0 = etat.get("dernier_passage") or maintenant()
    par = {}
    for a in ann:
        par.setdefault(a["recherche"], []).append(a)

    lignes = []
    for nom, lst in sorted(par.items()):
        lst = [a for a in lst if a.get("publie")]  # sans l'âge exact (premier passage), pas de statistiques
        termines = [a for a in lst if a["statut"] == "vendu" or t0 - (a.get("publie") or a["vu"]) >= 24 * 3600]
        vendus = [a for a in lst if a["statut"] == "vendu"]
        v24 = [a for a in vendus if (heures(a.get("publie"), a["vendu_vu"]) or 1e9) <= 24]
        delais = [h for h in (heures(a.get("publie"), a["vendu_vu"]) for a in vendus) if h is not None]
        prixv = [a["prix"] for a in vendus if a["prix"] is not None]
        taux = f"{100 * len(v24) / len(termines):.0f} %" if termines else "–"
        lignes.append(
            f"<tr><td>{html.escape(nom)}</td><td>{len(lst)}</td><td>{len(vendus)}</td><td>{len(v24)}</td><td>{taux}</td>"
            f"<td>{fmt_h(statistics.median(delais)) if delais else '–'}</td><td>{fmt_prix(statistics.median(prixv)) if prixv else '–'}</td></tr>")

    vendus = sorted([a for a in ann if a["statut"] == "vendu"], key=lambda a: a["vendu_vu"], reverse=True)
    cartes = []
    for a in vendus:
        h = heures(a.get("publie"), a["vendu_vu"])
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" '
               f'onerror="this.replaceWith(\'Photo expirée\')">') if a["photo"] else "Pas de photo"
        cartes.append(
            f'<article class="card" data-r="{html.escape(a["recherche"])}"><div class="ph">{img}<span class="b d{" hot" if h is not None and h <= 6 else ""}">≤ {fmt_h(h)}</span>'
            f'<span class="b p">{fmt_prix(a["prix"])}</span></div><div class="body"><span class="m">{html.escape(a["recherche"])}</span>'
            f'<span class="t">{html.escape(a["titre"])}</span><span class="m">{html.escape(" · ".join(x for x in (a["marque"], a["taille"], a["etat_article"]) if x))}'
            f'{" · ♥ " + str(a["likes"]) if a.get("likes") else ""}</span>'
            f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Voir l’annonce ↗</a></div></article>')

    maj = datetime.fromtimestamp(t0, ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>Ventes suivies</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icone-180.png"><meta name="theme-color" content="#2f5d50">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Ventes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Ventes suivies</h1><p class="sub">Mis à jour le {maj}. Une annonce est suivie pendant {FENETRE_SUIVI_H} h après sa publication.
Le délai « ≤ » est le temps entre la mise en ligne et le moment où la vente a été repérée : la vente réelle a eu lieu avant.</p></header>
<section><h2>Par recherche</h2><div class="tbl"><table><thead><tr><th>Recherche</th><th>Suivies</th><th>Vendues</th><th>En &lt; 24 h</th><th>Taux 24 h</th><th>Délai médian</th><th>Prix médian</th></tr></thead>
<tbody>{''.join(lignes) or '<tr><td colspan="7">Aucune donnée pour l’instant.</td></tr>'}</tbody></table></div>
<p class="sub">Taux 24 h : part des annonces vendues en moins de 24 h, parmi celles publiées depuis au moins 24 h ou déjà vendues.</p></section>
<section><h2>Vendues ({len(vendus)})</h2><div class="chips" id="chips"></div><div class="grid" id="grid">{''.join(cartes)}</div></section></div>
<script>(function(){{var g=document.getElementById('grid'),c=document.getElementById('chips'),n={{}};
g.querySelectorAll('.card').forEach(function(e){{n[e.dataset.r]=(n[e.dataset.r]||0)+1}});
var k=Object.keys(n);if(k.length<2)return;['' ].concat(k).forEach(function(r,i){{var b=document.createElement('button');b.type='button';
b.textContent=(r||'Tout')+' ('+(r?n[r]:g.children.length)+')';b.setAttribute('aria-pressed',i===0);
b.onclick=function(){{c.querySelectorAll('button').forEach(function(x){{x.setAttribute('aria-pressed',x===b)}});
g.querySelectorAll('.card').forEach(function(e){{e.hidden=!!r&&e.dataset.r!==r}})}};c.appendChild(b)}})}})();</script></body></html>"""
    os.makedirs(os.path.dirname(FICHIER_RAPPORT), exist_ok=True)
    with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- test

def diagnostic(url):
    """Affiche ce que le site renvoie, pour comprendre un échec, et l'envoie par notification."""
    global log
    lignes = []
    log_orig = log
    def log(*a):
        lignes.append(" ".join(str(x) for x in a))
        log_orig(*a)
    try:
        _diagnostic(url)
    finally:
        log = log_orig
    notifier("Diagnostic", "\n".join(lignes)[:3800], tags="mag")


def _diagnostic(url):
    log("---- DIAGNOSTIC ----")
    log(f"Base : {BASE}")
    log(f"Cookies : {', '.join(session.cookies.keys()) or 'aucun'}")
    p = params_depuis_url(url)
    essais = [
        ("api complet", BASE + "/api/v2/catalog/items", p),
        ("api simple", BASE + "/api/v2/catalog/items", {"search_text": p.get("search_text", "barbour"), "per_page": "5"}),
        ("api items", BASE + "/api/v2/items", {"search_text": p.get("search_text", "barbour"), "per_page": "5"}),
    ]
    for nom, u, prm in essais:
        try:
            r = session.get(u, params=prm, headers={"Accept": "application/json, text/plain, */*", "Referer": url}, timeout=25)
            log(f"[{nom}] HTTP {r.status_code} · {r.headers.get('content-type', '')} · {r.text[:200]!r}")
        except Exception as e:
            log(f"[{nom}] erreur {e}")
    try:
        r = session.get(url, headers={"Accept": "text/html"}, timeout=25)
        t = r.text
        ids = set(re.findall(r'/items/(\d{6,})', t))
        log(f"[page recherche] HTTP {r.status_code} · {len(t)} caractères · {len(ids)} annonces trouvées dans le HTML")
        for m in ("__NEXT_DATA__", "self.__next_f", '"catalogItems"', '"price"', '"photo"', "data-testid=\"grid-item\"", "product-item-id"):
            log(f"   contient {m} : {m in t}")
        for m in re.finditer(r'/api/v2/[a-z_/]+', t):
            log(f"   lien api vu : {m.group(0)}")
            break
    except Exception as e:
        log(f"[page recherche] erreur {e}")
    log("---- FIN DIAGNOSTIC ----")


def test():
    """Vérifie que tout fonctionne, sans rien enregistrer."""
    recherches = lire_recherches()
    if not recherches:
        log("Aucune recherche : ajoute le secret RECHERCHES.")
        notifier("Test : échec", "Le secret RECHERCHES est vide.", tags="warning")
        return 1
    ok = ouvrir_session()
    nom, url = recherches[0]
    log(f"Recherche testée : {nom}")
    log(f"Paramètres : {params_depuis_url(url)}")
    try:
        items = chercher(url)
    except Exception as e:
        log(f"ÉCHEC de la recherche : {e}")
        notifier("Test : échec", f"La recherche ne répond pas depuis GitHub ({e}).", tags="warning")
        return 1
    log(f"{len(items)} annonces reçues (méthode : {DERNIERE_METHODE})")
    exemples = []
    for it in items[:3]:
        pub = publie_le(it)
        exemples.append(f"{it.get('title')} | {fmt_prix(prix(it))} | {it.get('brand_title')} | {it.get('size_title')} | {it.get('status')} | publiée il y a {fmt_h(heures(pub, maintenant())) if pub else '?'} | photo {'oui' if (it.get('photo') or {}).get('url') else 'non'}")
    notifier("Exemples", f"Méthode : {DERNIERE_METHODE}\n" + "\n".join(exemples), tags="mag")
    notifier("Données brutes", EXEMPLE_BRUT or "aucune", tags="mag")
    for it in items[:5]:
        pub = publie_le(it)
        age = fmt_h(heures(pub, maintenant())) if pub else "?"
        log(f"  - {it.get('title')} | {fmt_prix(prix(it))} | {it.get('brand_title')} | publiée il y a {age}")
    if items:
        s = statut_page(items[0].get("url") or f"{BASE}/items/{items[0]['id']}")
        log(f"Statut lu sur la page de la première annonce : {s}")
    notifier("Test réussi", f"{len(items)} annonces trouvées pour « {nom} ». Les notifications fonctionnent.",
             lien=items[0].get("url") if items else None,
             photo=(items[0].get("photo") or {}).get("url") if items else None, tags="white_check_mark")
    return 0 if ok and items else 1


INTERVALLE_MIN = 15         # minutes entre deux passages d'un même lancement
INTERVALLE_NUIT_MIN = 60    # la nuit (NUIT_DEBUT h à NUIT_FIN h, heure de Paris)
NUIT_DEBUT, NUIT_FIN = 0, 7


def est_nuit():
    h = datetime.now(ZoneInfo("Europe/Paris")).hour
    return NUIT_DEBUT <= h < NUIT_FIN


def boucle(n):
    """Plusieurs passages d'affilée, espacés d'environ INTERVALLE_MIN minutes (avec un peu d'aléa)."""
    for k in range(n):
        debut = maintenant()
        try:
            passage()
        except Exception as e:
            log(f"Passage {k + 1} interrompu : {e}")
        if k < n - 1:
            pause = INTERVALLE_NUIT_MIN if est_nuit() else INTERVALLE_MIN
            attente = pause * 60 + random.uniform(-90, 90) - (maintenant() - debut)
            fin_nuit = datetime.now(ZoneInfo("Europe/Paris")).replace(hour=NUIT_FIN, minute=2, second=0, microsecond=0).timestamp()
            if est_nuit() and maintenant() + attente > fin_nuit:
                attente = max(60, fin_nuit - maintenant())  # reprendre le rythme de jour dès 7 h
            if attente > 0:
                log(f"Prochain passage dans {attente / 60:.0f} min")
                time.sleep(attente)


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(test())
    if "--boucle" in sys.argv:
        boucle(int(sys.argv[sys.argv.index("--boucle") + 1]))
    else:
        passage()
