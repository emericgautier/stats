"""Suivi des ventes par recherche, avec notification et rapport HTML.

Chaque passage :
  1. lit les recherches du secret RECHERCHES (une URL de recherche par ligne) ;
  2. ajoute au suivi les nouvelles annonces de chaque recherche ;
  3. revérifie les annonces suivies qui ont disparu des résultats ;
  4. envoie une notification ntfy pour chaque vente rapide ;
  5. régénère docs/index.html ;
  6. garde les meilleures ventes pour de bon (meilleures_ventes.json, page docs/meilleures/).

Aucune connexion à un compte : seulement des pages publiques.
"""

import csv
import html
import io
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER_RECHERCHES = os.path.join(ICI, "recherches.txt")  # repli local, hors dépôt
FICHIER_AJOUTEES = os.path.join(ICI, "recherches_ajoutees.txt")  # recherches en plus du secret, modifiables sur GitHub
FICHIER_PRIX_MIN = os.path.join(ICI, "prix_min.txt")  # prix minimum par recherche, modifiable sur GitHub
FICHIER_RETIREES = os.path.join(ICI, "recherches_retirees.txt")  # recherches à ne plus suivre
FICHIER_LIKES = os.path.join(ICI, "recherches_likes.txt")  # recherches lues seulement pour les likes des nouvelles annonces
FICHIER_ETAT = os.path.join(ICI, "etat.json")
FICHIER_ETAT_ANCIEN = os.path.join(ICI, "etat_ancien.json")  # mémoire d'avant le retrait de recherches, relue une seule fois
RESTAURER = ["Ba&sh sweat", "Sézane sweat", "Sézane trench"]  # recherches retirées dont on récupère les ventes passées
FICHIER_RAPPORT = os.path.join(ICI, "docs", "index.html")
FICHIER_BASE = os.path.join(ICI, "meilleures_ventes.json")  # meilleures ventes, gardées pour de bon (enregistrées dans le dépôt)
FICHIER_HISTO = os.path.join(ICI, "historique_ventes.json")  # ventes rapides des HISTO_J derniers jours
FICHIER_BASE_A_ENREGISTRER = os.path.join(ICI, ".base_a_enregistrer")
DOSSIER_BASE = os.path.join(ICI, "docs", "meilleures")       # page de la base et photos réduites
DOSSIER_EXPORT = os.path.join(ICI, "docs", "ventes")  # ventes en texte brut, une page par recherche, pour les analyses
EXPORT_LIGNES = 120         # ventes par fichier texte (pages courtes, lisibles d'un coup)

BASE = ""  # déduit de la première recherche
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# Réglages
FENETRE_SUIVI_H = 72        # durée de suivi d'une annonce après sa publication
NOTIF_MAX_H = 24            # notifier seulement les ventes parties en moins de X heures
PAGES_PAR_RECHERCHE = 1     # 96 annonces les plus récentes, largement assez toutes les 30 min
MAX_VERIFS_PAR_PASSAGE = 250 # pages d'annonces ouvertes au maximum par passage
PAUSE = (2.0, 4.0)          # pause entre deux pages d'annonces (secondes)
CONSERVATION_J = 10         # oubli des annonces plus anciennes
ETATS = ["2"]               # états gardés pour toutes les recherches (2 = très bon état)
CLASSEMENT_J = 7           # période du classement de la semaine
CLASSEMENT_MIN_VENTES = 3  # ventes minimum pour qu'une recherche soit classée
ACHAT_PART = 0.40          # prix d'achat max : 40 % du prix médian vendu…
ACHAT_MARGE = 30           # … en gardant au moins 30 € de marge
BASE_PAR_SEMAINE = 5       # meilleures ventes gardées pour de bon, par recherche et par semaine
BASE_MAX_H = 6             # … parmi celles parties en X heures au plus, au prix médian de leur recherche ou plus
HISTO_J = 60               # ventes rapides gardées pour reconnaître les modèles sûrs
MODELE_MIN_VENTES = 3      # modèle sûr : vendu au moins X fois en BASE_MAX_H h au plus…
MODELE_ECART = 0.25        # … avec au moins 2 ventes sur 3 à ± 25 % du prix médian
BASE_ENREGISTRER_H = 3     # la base est enregistrée dans le dépôt au plus toutes les X heures
COPIE_MARQUE = False        # True : copie de chaque vente sur le sujet de sa marque (sujet-marque) en plus du sujet principal
RESUME_JOUR, RESUME_HEURE = 0, 8  # résumé de la semaine le lundi (0) à partir de 8 h
LIKES_PAGES = 3            # pages lues par recherche « likes » (96 annonces par page, les plus récentes)
LIKES_GARDE_H = 24         # annonces gardées X heures après leur mise en ligne
LIKES_MIN = 3              # likes minimum pour apparaître dans « Likes rapides »
LIKES_AFFICHEES = 60       # cartes affichées au plus

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
        if k in ("time", "search_id", "page", "per_page", "order", "disabled_personalization", "marque"):
            continue
        p.setdefault(k, []).append(v)
    p = {k: ",".join(v) for k, v in p.items()}
    p.update({"order": "newest_first", "per_page": "96"})
    return p


def url_page(url, page):
    """Même recherche, triée des plus récentes, à la page voulue."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in ("page", "order", "time", "search_id", "marque")]
    q += [("order", "newest_first"), ("page", str(page))]
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def garder_marque(items, url):
    """Paramètre maison « marque=… » dans l'adresse : ne garde que les annonces de cette marque exacte
    (utile quand le mot-clé est un mot courant, ex. Soeur)."""
    m = dict(parse_qsl(urlsplit(url).query)).get("marque", "").strip().lower()
    if not m:
        return items
    return [it for it in items if (it.get("brand_title") or "").strip().lower() == m]


ENFANT_TITRE = re.compile(
    r"\b(enfants?|b[ée]b[ée]s?|fille|fillette|gar[çc]on(net)?s?|kids?|junior|juniors|baby|boys?|girls?|kinder|m[äa]dchen|jungen|ni[ñn][oa]s?|bambin[oa]|youth"
    r"|\d{1,2}\s*(ans|mois|months?|years?|jahre|anni|años))\b", re.I)
ENFANT_TAILLE = re.compile(r"\d{1,2}\s*(ans|mois|months?|years?)|\d{2,3}\s*cm|(5[0-9]|[6-9][0-9]|1[0-7][0-9])", re.I)  # 50 à 179 = taille en cm


def est_enfant(a):
    """Article enfant (titre ou taille) : jamais suivi ni affiché."""
    return bool(ENFANT_TITRE.search(a.get("titre") or a.get("title") or "")
                or ENFANT_TAILLE.fullmatch((a.get("taille") or a.get("size_title") or "").split("/")[0].strip()))


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
    global VITRINE_IDS, GRILLE_IDS
    soup = BeautifulSoup(texte, "html.parser")
    out, vus = [], set()
    # Sliders « Voir le vendeur » (dressing mis en avant) : ces annonces ne font pas partie des résultats
    VITRINE_IDS, GRILLE_IDS = set(), set()
    reperes = [e for e in soup.find_all(["a", "button"]) if re.search(r"voir le (vendeur|dressing)", e.get_text(" "), re.I)]
    reperes += soup.select('[data-testid*="closet" i]')
    for e in reperes:
        c = e.parent
        for _ in range(12):
            if c is None or c.select('[data-testid^="product-item-id-"]'):
                break
            c = c.parent
        if c is None:
            continue
        ids = {re.search(r"product-item-id-(\d+)", b.get("data-testid", "")).group(1) for b in c.select('[data-testid^="product-item-id-"]')}
        if len(ids) <= 40:
            VITRINE_IDS |= ids
    for bloc in soup.select('[data-testid^="product-item-id-"]'):
        m = re.search(r"product-item-id-(\d+)", bloc.get("data-testid", ""))
        if not m or m.group(1) in vus:
            continue
        i = m.group(1)
        vus.add(i)
        GRILLE_IDS.add(i)
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
VITRINE_IDS, GRILLE_IDS = set(), set()


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


def couleur_page(soup):
    """Couleur indiquée par le vendeur (ex. « Marron », « Noir, Blanc »)."""
    el = soup.select_one('[data-testid="item-attributes-color"]')
    if not el:
        return ""
    return re.sub(r"^\s*Couleurs?\s*:?\s*", "", el.get_text(" ", strip=True), flags=re.I).strip()


def statut_page(url, infos=None):
    """Ouvre la page publique d'une annonce : vendu, réservé, dispo ou supprimé.
    Si `infos` est un dict, y ajoute la couleur lue sur la page."""
    r = session.get(url, headers={"Accept": "text/html", "Referer": BASE + "/"}, timeout=25, allow_redirects=True)
    if r.status_code in (404, 410):
        return "supprime"
    if r.status_code == 429:
        time.sleep(60)
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    if infos is not None:
        infos["couleur"] = couleur_page(soup)
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

def marque_cle(nom_recherche):
    """Marque d'une recherche (premier mot du nom), pour son sujet ntfy : « Sézane sweat » → sezane."""
    mot = (nom_recherche.split() or [""])[0]
    mot = unicodedata.normalize("NFKD", mot).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", mot) or "autre"


def notifier(titre, message, lien=None, photo=None, tags="moneybag", marque=None):
    """Envoie sur le sujet principal ; avec COPIE_MARQUE, aussi en silence sur le sujet de la marque (sujet-marque)."""
    if not NTFY_TOPIC:
        log(f"   (pas de NTFY_TOPIC) {titre} — {message}")
        return
    h = {"Title": titre.encode("utf-8"), "Tags": tags}
    if lien:
        h["Click"] = lien
    if photo:
        h["Attach"] = photo
    for sujet in [NTFY_TOPIC] + ([f"{NTFY_TOPIC}-{marque}"] if marque and COPIE_MARQUE else []):
        # Une seule alerte : la copie sur le sujet de la marque arrive sans son ni vibration (priorité minimale)
        hs = h if sujet == NTFY_TOPIC else {**h, "Priority": "min"}
        try:
            requests.post(f"https://ntfy.sh/{sujet}", data=message.encode("utf-8"), headers=hs, timeout=15)
        except requests.RequestException as e:
            log(f"   Notification non envoyée ({sujet}) : {e}")

def par_marque(annonces):
    out = {}
    for a in annonces:
        out.setdefault(marque_cle(a["recherche"]), []).append(a)
    return out


# ---------------------------------------------------------------- état

def lire_recherches():
    global BASE
    out = []
    texte = os.environ.get("RECHERCHES", "")
    if not texte.strip() and os.path.exists(FICHIER_RECHERCHES):
        with open(FICHIER_RECHERCHES, encoding="utf-8") as f:
            texte = f.read()
    if os.path.exists(FICHIER_AJOUTEES):
        # Recherches ajoutées dans le dépôt, en plus de celles du secret
        with open(FICHIER_AJOUTEES, encoding="utf-8") as f:
            texte += "\n" + f.read()
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
    # Une recherche du fichier qui porte le nom d'une recherche du secret la remplace
    out = list({nom: (nom, url) for nom, url in out}.values())
    retirees = lire_retirees()
    out = [(nom, url) for nom, url in out if nom not in retirees]
    prix_min = lire_prix_min()
    out = [(nom, avec_prix_min(url, prix_min[nom]) if nom in prix_min else url) for nom, url in out]
    out = [(nom, avec_etats(url)) for nom, url in out]
    if out and not BASE:
        u = urlsplit(out[0][1])
        BASE = f"{u.scheme}://{u.netloc}"
    return out


def lire_recherches_likes():
    """Recherches « likes » : une ligne « Nom | adresse Vinted », prise telle quelle (états et prix de l'adresse)."""
    out = []
    if os.path.exists(FICHIER_LIKES):
        with open(FICHIER_LIKES, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "|" not in ligne:
                    continue
                nom, url = [x.strip() for x in ligne.split("|", 1)]
                out.append((nom, url))
    return out


def est_pro(it):
    """Vendeur professionnel (boutique), d'après les données de l'annonce."""
    u = it.get("user") if isinstance(it.get("user"), dict) else {}
    for d in (it, u):
        for k in ("business", "is_business", "isBusiness", "business_user", "businessUser", "is_business_user"):
            if d.get(k) is True:
                return True
    return False


def ages_corriges(items, ancres=None):
    """Date de mise en ligne de chaque annonce.
    Repères : date des photos quand elle existe, sinon heure à laquelle le suivi a vu l'annonce apparaître (ancres).
    Les numéros d'annonce croissent avec le temps : on en déduit une date pour chaque annonce, et on garde
    la plus ancienne des deux (une annonce remontée ou aux photos changées garde son vieux numéro)."""
    ancres = ancres or {}
    def repere(it):
        return publie_le(it) or ancres.get(str(it["id"])) or None
    pts = sorted((int(it["id"]), repere(it)) for it in items if repere(it))
    est = {}
    if len(pts) >= 20:
        n = len(pts) // 3
        bas, haut = pts[:n], pts[-n:]
        di = statistics.median(i for i, _ in haut) - statistics.median(i for i, _ in bas)
        dt = statistics.median(t for _, t in haut) - statistics.median(t for _, t in bas)
        if di > 0 and dt > 0:
            b = dt / di
            a = statistics.median(t - b * i for i, t in pts)
            for it in items:
                est[int(it["id"])] = a + b * int(it["id"])
    out = {}
    for it in items:
        i = int(it["id"])
        ts = repere(it)
        e = est.get(i)
        if e is not None and (ts is None or ts > e + 6 * 3600):
            ts = e
        out[str(i)] = ts
    return out


def passage_likes(etat, t0):
    """Relève les likes des nouvelles annonces des recherches « likes » (sans revérifier les ventes)."""
    recherches = lire_recherches_likes()
    lk = etat.setdefault("likes", {})
    vus = etat.setdefault("likes_vus", {})          # id -> heure d'apparition (0 : déjà là au premier relevé)
    init = set(etat.setdefault("likes_recherches", []))
    for i in [i for i, a in lk.items() if not a.get("publie") or t0 - a["publie"] > LIKES_GARDE_H * 3600]:
        del lk[i]
    for i in [i for i, v in vus.items() if v and t0 - v > 3 * 86400]:
        del vus[i]
    for nom, url in recherches:
        lus = []
        try:
            for page in range(1, LIKES_PAGES + 1):
                items = chercher(url, page)
                if GRILLE_IDS:
                    items = [it for it in items if str(it["id"]) in GRILLE_IDS]
                lus += [it for it in items if str(it["id"]) not in VITRINE_IDS]
                if len(items) < 20:
                    break
                time.sleep(random.uniform(*PAUSE))
        except Exception as e:
            log(f"[likes · {nom}] erreur de recherche : {e}")
            continue
        for it in lus:
            i = str(it["id"])
            if i not in vus:
                vus[i] = t0 if nom in init else 0
        ages = ages_corriges(lus, {str(it["id"]): vus.get(str(it["id"])) for it in lus})
        n = 0
        for it in lus:
            i = str(it["id"])
            pub = ages.get(i)
            if est_enfant(it) or est_pro(it) or it.get("promoted") or it.get("is_promoted") or not pub or t0 - pub > LIKES_GARDE_H * 3600:
                lk.pop(i, None)
                continue
            a = lk.get(i) or annonce_depuis_api(it, nom)
            a["publie"] = min(pub, a.get("publie") or pub)
            likes = it.get("favourite_count")
            if likes is not None:
                a["likes"] = likes
            a["maj"] = t0
            lk[i] = a
            n += 1
        if nom not in init:
            etat["likes_recherches"].append(nom)
        photos = sum(1 for it in lus if publie_le(it))
        avec_likes = sum(1 for it in lus if it.get("favourite_count") is not None)
        log(f"[likes · {nom}] {len(lus)} annonces lues ({photos} avec date photo, {avec_likes} avec likes), {n} de moins de {LIKES_GARDE_H} h")


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


def lire_retirees():
    """Noms des recherches à ignorer (un par ligne)."""
    if not os.path.exists(FICHIER_RETIREES):
        return set()
    with open(FICHIER_RETIREES, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip() and not l.strip().startswith("#")}


def avec_etats(url):
    """Impose les états de ETATS à l'adresse de recherche, quels que soient ceux qu'elle contient."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in ("status_ids[]", "status_ids")] + [("status_ids[]", e) for e in ETATS]
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


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


def restaurer_ventes(etat):
    """Remet une fois les ventes des recherches de RESTAURER effacées lors de leur retrait."""
    if etat.get("ventes_restaurees") or not os.path.exists(FICHIER_ETAT_ANCIEN):
        return
    try:
        with open(FICHIER_ETAT_ANCIEN, encoding="utf-8") as f:
            ancien = json.load(f).get("annonces", {})
    except (OSError, ValueError):
        return
    ann = etat["annonces"]
    remises = {i: a for i, a in ancien.items()
               if a.get("recherche") in RESTAURER and a.get("statut") == "vendu" and i not in ann}
    ann.update(remises)
    etat["ventes_restaurees"] = True
    log(f"{len(remises)} ventes passées remises ({', '.join(RESTAURER)})")


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


# ---------------------------------------------------------------- classement

def prix_achat_max(median):
    return max(0, min(ACHAT_PART * median, median - ACHAT_MARGE))


def classement(etat, t0):
    """Recherches de la semaine, de la plus rapide à vendre à la moins rapide, avec leur prix d'achat max."""
    debut = t0 - CLASSEMENT_J * 86400
    par = {}
    for a in etat["annonces"].values():
        if a.get("publie") and a["publie"] >= debut:
            par.setdefault(a["recherche"], []).append(a)
    out = []
    for nom, lst in par.items():
        vendus = [a for a in lst if a["statut"] == "vendu" and a["prix"] is not None]
        if len(vendus) < CLASSEMENT_MIN_VENTES:
            continue
        termines = [a for a in lst if a["statut"] == "vendu" or t0 - a["publie"] >= 24 * 3600]
        v24 = [a for a in vendus if (heures(a["publie"], a["vendu_vu"]) or 1e9) <= 24]
        med = statistics.median(a["prix"] for a in vendus)
        achat = prix_achat_max(med)
        out.append({
            "nom": nom, "vendus": len(vendus), "v24": len(v24),
            "taux": len(v24) / len(termines) if termines else 0,
            "median": med, "achat": achat,
            "exemples": sorted(vendus, key=lambda a: heures(a["publie"], a["vendu_vu"]) or 1e9)[:3],
        })
    # Les ventes rapides comptent d'abord, puis la part vendue en moins de 24 h
    out.sort(key=lambda c: (c["v24"] * c["taux"], c["v24"], c["median"]), reverse=True)
    return out


def adresse_page():
    depot = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in depot:
        return None
    proprio, nom = depot.split("/", 1)
    return f"https://{proprio.lower()}.github.io/{nom}/"


def resume_semaine(etat, t0):
    """Une fois par semaine : les 5 meilleures recherches et leur prix d'achat max, sur le sujet principal."""
    ici = datetime.fromtimestamp(t0, ZoneInfo("Europe/Paris"))
    semaine = ici.strftime("%G-%V")
    if ici.weekday() != RESUME_JOUR or ici.hour < RESUME_HEURE or etat.get("resume_semaine") == semaine:
        return
    cl = classement(etat, t0)[:5]
    if not cl:
        return
    lignes = [f"{k}. {c['nom']} · {c['v24']} vendues < 24 h ({100 * c['taux']:.0f} %) · médian {fmt_prix(c['median'])} → achat ≤ {fmt_prix(round(c['achat']))}"
              for k, c in enumerate(cl, 1)]
    photo = next((a["photo"] for a in cl[0]["exemples"] if a["photo"]), None)
    notifier("Classement de la semaine", "\n".join(lignes), lien=adresse_page(), photo=photo, tags="trophy")
    etat["resume_semaine"] = semaine


# ---------------------------------------------------------------- passage

def passage():
    etat = lire_etat()
    ann = etat["annonces"]
    t0 = maintenant()
    recherches = lire_recherches()
    if not recherches:
        log("Aucune recherche : ajoute le secret RECHERCHES.")
        return
    restaurer_ventes(etat)
    # Articles enfant : retirés partout (page, statistiques, notifications)
    for i in [i for i, a in ann.items() if est_enfant(a)]:
        del ann[i]
    # Annonces des recherches retirées : plus de vérification ni de notification, seules les ventes restent sur la page
    retirees = lire_retirees()
    for i in [i for i, a in ann.items() if a["recherche"] in retirees and a["statut"] != "vendu"]:
        del ann[i]
    ouvrir_session()

    presents = set()
    deja_vues = set(etat.get("recherches_vues", []))
    for nom, url in recherches:
        nouveaux = 0
        try:
            for page in range(1, PAGES_PAR_RECHERCHE + 1):
                items = chercher(url, page)
                for it in garder_marque(items, url):
                    if est_enfant(it):
                        continue
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

    try:
        passage_likes(etat, t0)
    except Exception as e:
        log(f"Likes rapides : {e}")

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
            infos = {}
            s = statut_page(a["url"], infos)
            if infos.get("couleur"):
                a["couleur"] = infos["couleur"]
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
        for marque, lst in par_marque(nuit).items():
            lst.sort(key=lambda a: heures(a.get("publie"), a["vendu_vu"]) or 1e9)
            lignes = [f"{fmt_prix(a['prix'])} · {fmt_h(heures(a.get('publie'), a['vendu_vu']))} · {a['recherche']} · {a['titre']}" for a in lst[:10]]
            notifier(f"Cette nuit : {len(lst)} vente{'s' if len(lst) > 1 else ''} rapide{'s' if len(lst) > 1 else ''} · {lst[0]['recherche'].split()[0]}",
                     "\n".join(lignes), lien=lst[0]["url"], photo=lst[0]["photo"], tags="sunrise", marque=marque)
    # Une notification par vente, sur le sujet de sa marque ; regroupées au-delà de 5 pour une même marque
    for marque, lst in par_marque(rapides).items():
        if len(lst) > 5:
            lignes = [f"{fmt_prix(a['prix'])} · {fmt_h(heures(a.get('publie'), a['vendu_vu']))} · {a['recherche']} · {a['titre']}" for a in lst[:8]]
            notifier(f"{len(lst)} ventes rapides · {lst[0]['recherche'].split()[0]}", "\n".join(lignes), tags="fire", marque=marque)
            continue
        for a in lst:
            delai = fmt_h(heures(a.get("publie"), a["vendu_vu"]))
            details = " · ".join(x for x in (a["marque"], a["taille"], a["etat_article"]) if x)
            notifier(f"{fmt_prix(a['prix'])} · VENDU en {delai} · {a['recherche']}",
                     a["titre"] + (f"\n{details}" if details else ""),
                     lien=a["url"], photo=a["photo"], marque=marque)

    # Ménage
    limite = t0 - CONSERVATION_J * 86400
    for i in [i for i, a in ann.items() if (a.get("publie") or a["vu"]) < limite]:
        del ann[i]

    if not est_nuit():
        resume_semaine(etat, t0)

    etat["dernier_passage"] = t0
    ecrire_etat(etat)
    try:
        maj_base(etat, t0)
    except Exception as e:
        log(f"Base des meilleures ventes : {e}")
    ecrire_rapport(etat)
    ecrire_export(etat)
    ecrire_likes_json(etat)


# ---------------------------------------------------------------- rapport

CSS = """.haut{display:flex;align-items:center;justify-content:space-between;gap:12px}.tirer{position:fixed;top:0;left:50%;transform:translate(-50%,-48px);z-index:5;font-size:13px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:6px 12px;pointer-events:none}.boutons{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.boutons a{font-size:14px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:8px 14px;text-decoration:none}.haut button{font:inherit;font-size:14px;font-weight:600;color:var(--accent);background:var(--accent-soft);border:0;border-radius:99px;padding:8px 14px;cursor:pointer}
:root{--bg:#f6f6f3;--surface:#fff;--ink:#1d2120;--muted:#646b68;--line:#dfe1dc;--accent:#2f5d50;--accent-soft:#e3ece8;--hot:#9a3b12;color-scheme:light}
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
.filtres{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.rech{position:relative;flex:1 1 100%}.rech input{width:100%;font:600 16px system-ui;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 12px}
.sug{position:absolute;z-index:5;left:0;right:0;top:calc(100% + 4px);background:var(--surface);border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.18);overflow:hidden}
.sug button{display:flex;justify-content:space-between;width:100%;font:600 16px system-ui;color:var(--ink);background:none;border:0;border-bottom:1px solid var(--line);padding:12px;text-align:left;cursor:pointer}
.sug button:last-child{border-bottom:0}.sug button span{color:var(--muted);font-weight:400}.sug button:hover,.sug button:focus{background:var(--accent-soft)}.sug p{margin:0;padding:12px;color:var(--muted)}.filtres select{flex:1 1 220px;min-width:0;font:600 16px system-ui;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 12px}
[hidden],.card.masq{display:none!important}
.cache{position:absolute;top:8px;left:8px;width:28px;height:28px;border-radius:99px;border:0;background:rgba(0,0,0,.55);color:#fff;font:600 18px/28px system-ui;padding:0;cursor:pointer}
.remontrer{font:600 14px system-ui;color:var(--accent);background:var(--accent-soft);border:0;border-radius:8px;padding:9px 12px;cursor:pointer}
img[data-u]{cursor:zoom-in}.vue{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.92);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom))}
.vph{position:relative;display:flex;max-width:100%;max-height:calc(100% - 130px)}.vph img{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px}
.vph .d{font-size:14px}.vue .fl{position:absolute;top:50%;transform:translateY(-50%);font:600 40px/1 system-ui;color:#fff;background:rgba(255,255,255,.14);border:0;border-radius:99px;width:48px;height:48px;padding:0;cursor:pointer}.vue .fl.pv{left:8px;right:auto}.vue .fl.nx{right:8px}
.vue{touch-action:pan-y}.vue p b{font-size:18px}.vue p em.co{background:#fff;color:#1d2120}.vue p em{font-style:normal;font-weight:600;background:#2f5d50;border-radius:99px;padding:2px 9px;margin-left:6px;white-space:nowrap}.vue p{color:#fff;margin:0;font-size:14px;text-align:center}
.vbt{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:center}.vue .vbt button{position:static;font:600 15px system-ui;color:#fff;background:rgba(255,255,255,.18);border:0;border-radius:999px;padding:10px 16px;cursor:pointer}.vbt .auto.on{background:#2f5d50}.vbt .vit[hidden]{display:none}.vue a{background:#fff;color:#1d2120;font-weight:700;text-decoration:none;border-radius:999px;padding:10px 20px}.vue button{position:absolute;top:max(12px,env(safe-area-inset-top));right:12px;font:600 28px/1 system-ui;color:#fff;background:none;border:0;padding:8px}
.vig{display:flex;gap:4px}.vig a{display:block;width:44px;height:58px;border-radius:4px;overflow:hidden;background:var(--accent-soft)}.vig img{width:100%;height:100%;object-fit:cover}
.achat{font-weight:700;color:var(--accent)}.ventes{display:flex;gap:4px;flex-wrap:wrap}.body .ventes a{margin:0;font-size:12px;font-weight:600;color:var(--ink);background:var(--accent-soft);border-radius:4px;padding:1px 6px}
.repli summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}.repli summary::-webkit-details-marker{display:none}
.repli summary h2{margin:0}.repli summary::after{content:"▾ afficher";font-size:13px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:3px 10px}
.repli[open] summary::after{content:"▴ masquer"}.repli[open] summary{margin-bottom:8px}
@media (max-width:520px){.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:24px}}"""


# Garde la page telle qu'on l'a laissée (menu marque, sections ouvertes, défilement, photo ouverte)
# quand on revient de Vinted ou qu'elle se recharge toute seule.
SCRIPT_ETAT = """<script>(function(){var K='page_etat',c=document.getElementById('f'),v=document.getElementById('vue'),pret=false;
try{history.scrollRestoration='manual'}catch(e){}
function base(o){return o?o.textContent.replace(/\\s*\\(\\d+\\)$/,''):''}
function sections(){return [].map.call(document.querySelectorAll('details.repli'),function(d){return d.querySelector('summary').textContent.replace(/\\s*\\(\\d+\\)$/,'')})}
function repere(){var L=document.querySelectorAll('.card:not([hidden]):not(.masq) img[data-u]');
for(var i=0;i<L.length;i++){var r=L[i].closest('.card').getBoundingClientRect();if(r.bottom>0&&r.height)return {u:L[i].dataset.u,y:r.top}}return null}
function garder(){if(!pret)return;var d=document.querySelectorAll('details.repli'),n=sections(),o=[];
d.forEach(function(x,i){if(x.open)o.push(n[i])});
var e={t:Date.now(),m:c&&!c.hidden?base(c.options[c.selectedIndex]):'',o:o,y:window.scrollY,a:repere(),v:v&&!v.hidden?v.querySelector('a').getAttribute('href'):''};
try{localStorage.setItem(K,JSON.stringify(e))}catch(x){}}
function trouver(u){var L=document.querySelectorAll('img[data-u]');for(var i=0;i<L.length;i++)if(L[i].dataset.u===u)return L[i];return null}
function placer(e){var x=e.a&&trouver(e.a.u),c2=x&&x.closest('.card');
if(c2&&!c2.hidden&&!c2.classList.contains('masq'))window.scrollTo(0,window.scrollY+c2.getBoundingClientRect().top-e.a.y);else window.scrollTo(0,e.y||0)}
var e=null;try{e=JSON.parse(localStorage.getItem(K)||'null')}catch(x){}
if(e){if(c&&!c.hidden&&e.m){for(var i=0;i<c.options.length;i++)if(base(c.options[i])===e.m){c.value=c.options[i].value;c.onchange&&c.onchange();c.dispatchEvent(new Event('change'));break}}
var n=sections();document.querySelectorAll('details.repli').forEach(function(d,i){d.open=(e.o||[]).indexOf(n[i])>=0});
// Défilement et photo ouverte : seulement si on revient dans les 6 heures
if(Date.now()-e.t<6*3600e3){placer(e);requestAnimationFrame(function(){placer(e)});setTimeout(function(){placer(e)},400);
if(e.v){var im=trouver(e.v);if(im)im.click()}}}
pret=true;var tm=null;
window.addEventListener('scroll',function(){clearTimeout(tm);tm=setTimeout(garder,250)},{passive:true});
document.addEventListener('change',garder);document.addEventListener('click',function(){setTimeout(garder,0)});
document.querySelectorAll('details.repli').forEach(function(d){d.addEventListener('toggle',garder)});
document.addEventListener('visibilitychange',function(){if(document.hidden)garder()});
window.addEventListener('pagehide',garder)})();</script>"""


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

    def zoom(a):
        """Données de la visionneuse : la photo s'ouvre en grand, avec un bouton vers l'annonce."""
        return f'data-u="{html.escape(a["url"])}" data-t="{html.escape(a["titre"])} · {fmt_prix(a["prix"])}"'

    vendus = sorted([a for a in ann if a["statut"] == "vendu"], key=lambda a: a["vendu_vu"], reverse=True)
    cl = classement(etat, t0)
    achat_max = {c["nom"]: round(c["achat"]) for c in cl}
    cartes = []
    for a in vendus:
        h = heures(a.get("publie"), a["vendu_vu"])
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)} '
               f'onerror="this.replaceWith(\'Photo expirée\')">') if a["photo"] else "Pas de photo"
        cartes.append(
            f'<article class="card" data-id="{html.escape(a["id"])}" data-r="{html.escape(a["recherche"])}" data-m="{html.escape((a["marque"] or "").strip())}" data-h="{html.escape(fmt_h(h))}" data-s="{html.escape(a["taille"] or "")}" data-c="{html.escape(a.get("couleur") or "")}"{f' data-x="achat max ≤ {fmt_prix(achat_max[a["recherche"]])}"' if a["recherche"] in achat_max else ""}><div class="ph">{img}<button type="button" class="cache" aria-label="Masquer cette vente" title="Masquer cette vente">×</button><span class="b d{" hot" if h is not None and h <= 6 else ""}">≤ {fmt_h(h)}</span>'
            f'<span class="b p">{fmt_prix(a["prix"])}</span></div><div class="body"><span class="m">{html.escape(a["recherche"])}</span>'
            f'<span class="t">{html.escape(a["titre"])}</span><span class="m">{html.escape(" · ".join(x for x in (a["marque"], a["taille"], a.get("couleur"), a["etat_article"]) if x))}'
            f'{" · ♥ " + str(a["likes"]) if a.get("likes") else ""}</span>'
            f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Voir l’annonce ↗</a></div></article>')

    def vignette(a):
        img = f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)}>' if a["photo"] else ""
        return f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener" title="{html.escape(a["titre"])} · {fmt_prix(a["prix"])}">{img}</a>'
    lignes_cl = [
        f"<tr><td>{k}</td><td>{html.escape(c['nom'])}</td><td class=\"achat\">≤ {fmt_prix(round(c['achat']))}</td><td>{fmt_prix(c['median'])}</td>"
        f"<td>{c['v24']} / {c['vendus']}</td><td>{100 * c['taux']:.0f} %</td>"
        f"<td><div class=\"vig\">{''.join(vignette(a) for a in c['exemples'])}</div></td></tr>"
        for k, c in enumerate(cl, 1)]
    section_cl = (
        f"""<section><details class="repli"><summary><h2>Classement de la semaine</h2></summary><div class="tbl"><table><thead><tr><th>#</th><th>Recherche</th><th>Achat max</th><th>Prix médian vendu</th>"""
        f"""<th>Vendues &lt; 24 h</th><th>Taux 24 h</th><th>Plus rapides</th></tr></thead><tbody>"""
        f"""{''.join(lignes_cl) or f'<tr><td colspan="7">Pas encore assez de ventes (au moins {CLASSEMENT_MIN_VENTES} par recherche sur {CLASSEMENT_J} jours).</td></tr>'}</tbody></table></div>"""
        f"""<p class="sub">Sur les {CLASSEMENT_J} derniers jours. Achat max : {ACHAT_PART * 100:.0f} % du prix médian vendu, avec au moins {ACHAT_MARGE} € de marge. Les vignettes sont les ventes les plus rapides.</p></details></section>""")

    # Couleurs qui partent vite, par marque (la couleur est lue sur la page de l'annonce vendue)
    pc = {}
    for a in vendus:
        if a.get("couleur") and a.get("marque"):
            pc.setdefault(a["marque"].strip(), {}).setdefault(a["couleur"], []).append(a)
    lignes_c = []
    for m in sorted(pc, key=lambda m: m.lower()):
        cs = []
        for coul, lst in pc[m].items():
            if len(lst) < 2:
                continue
            v24 = sum(1 for a in lst if (heures(a.get("publie"), a["vendu_vu"]) or 1e9) <= 24)
            px = [a["prix"] for a in lst if a["prix"] is not None]
            cs.append((v24, len(lst), coul, statistics.median(px) if px else None))
        for v24, n, coul, med in sorted(cs, reverse=True)[:4]:
            lignes_c.append(f"<tr><td>{html.escape(m)}</td><td>{html.escape(coul)}</td><td>{v24} / {n}</td><td>{fmt_prix(med) if med is not None else '–'}</td></tr>")
    section_couleurs = (
        """<section><details class="repli"><summary><h2>Couleurs</h2></summary><div class="tbl"><table><thead><tr><th>Marque</th><th>Couleur</th><th>Vendues &lt; 24 h</th><th>Prix médian</th></tr></thead><tbody>"""
        f"""{''.join(lignes_c) or '<tr><td colspan="4">Pas encore assez de ventes avec leur couleur (au moins 2 par couleur).</td></tr>'}</tbody></table></div>"""
        """<p class="sub">Jusqu’à 4 couleurs par marque, des plus vendues en moins de 24 h aux moins vendues. La couleur est celle indiquée par le vendeur.</p></details></section>""")

    section_modeles = (
        f"""<section><details class="repli"><summary><h2>Modèles sûrs ({len(MODELES)})</h2></summary>{html_modeles(MODELES[:24], "meilleures/photos/")}"""
        f"""{f'<p class="sub"><a href="meilleures/">Voir les {len(MODELES)} modèles sûrs ↗</a></p>' if len(MODELES) > 24 else ''}"""
        f"""<p class="sub">{texte_modeles()}</p></details></section>""")

    section_likes = html_likes(etat, t0, zoom)

    maj = datetime.fromtimestamp(t0, ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>Ventes suivies</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icone-180.png"><meta name="theme-color" content="#2f5d50">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Ventes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><style>{CSS}</style></head><body><div class="tirer" id="tirer">↓ Tirer pour actualiser</div><div class="wrap">
<header><div class="haut"><h1>Ventes suivies</h1><div class="boutons"><a href="meilleures/">Meilleures</a><a href="analyses/">Analyses</a><button type="button" id="actualiser">↻ Actualiser</button></div></div><p class="sub">Mis à jour le {maj}. Une annonce est suivie pendant {FENETRE_SUIVI_H} h après sa publication.
Le délai « ≤ » est le temps entre la mise en ligne et le moment où la vente a été repérée : la vente réelle a eu lieu avant.</p></header>
{section_likes}
{section_modeles}
{section_cl}
{section_couleurs}
<section><details class="repli"><summary><h2>Par recherche</h2></summary><div class="tbl"><table><thead><tr><th>Recherche</th><th>Suivies</th><th>Vendues</th><th>En &lt; 24 h</th><th>Taux 24 h</th><th>Délai médian</th><th>Prix médian</th></tr></thead>
<tbody>{''.join(lignes) or '<tr><td colspan="7">Aucune donnée pour l’instant.</td></tr>'}</tbody></table></div>
<p class="sub">Taux 24 h : part des annonces vendues en moins de 24 h, parmi celles publiées depuis au moins 24 h ou déjà vendues.</p></details></section>
<section><h2>Vendues ({len(vendus)})</h2><div class="filtres"><div class="rech" id="rech" hidden><input type="search" id="q" placeholder="Chercher une marque…" aria-label="Chercher une marque" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="search"><div class="sug" id="sug" hidden></div></div><select id="f" aria-label="Marque ou recherche" hidden></select></div><div class="filtres badges" id="bdg" hidden></div><div class="grid" id="grid">{''.join(cartes)}</div></section></div>
<div class="vue" id="vue" hidden><button type="button" class="x" aria-label="Fermer">×</button><button type="button" class="fl pv" aria-label="Précédente">‹</button><button type="button" class="fl nx" aria-label="Suivante">›</button><div class="vph"><img alt="" referrerpolicy="no-referrer"><span class="b d"></span></div><p></p><div class="vbt"><button type="button" class="auto" aria-label="Défilement automatique">▶ Auto</button><button type="button" class="vit" aria-label="Vitesse du défilement">3 s</button><a target="_blank" rel="noopener">Voir sur Vinted ↗</a></div></div>
<script>(function(){{var g=document.getElementById('grid'),cartes=[].slice.call(g.querySelectorAll('.card')),c=document.getElementById('f'),choix=[],par={{}};
// Un seul menu : toutes les ventes, puis une ligne par marque
cartes.forEach(function(e){{var m=(e.dataset.m||'').trim(),k=m.toLowerCase();if(!k)return;var b=par[k]||(par[k]={{nom:m,n:0,r:{{}}}});b.n++;b.r[e.dataset.r]=(b.r[e.dataset.r]||0)+1}});
function ajouter(texte,f){{var o=document.createElement('option');o.value=choix.length;o.textContent=texte;choix.push(f);c.appendChild(o)}}
ajouter('Toutes les ventes ('+cartes.length+')',null);
var ks=Object.keys(par).sort(function(x,y){{return par[x].nom.localeCompare(par[y].nom,'fr')}});
ks.forEach(function(k){{var b=par[k];ajouter(b.nom+' ('+b.n+')',{{m:k}})}});
if(ks.length>1||choix.length>2){{c.hidden=false;c.onchange=function(){{var f=choix[+c.value];
cartes.forEach(function(e){{e.hidden=!!f&&((e.dataset.m||'').trim().toLowerCase()!==f.m||(!!f.r&&e.dataset.r!==f.r))}})}};
// Recherche rapide : taper le début d'une marque puis toucher la suggestion
var rq=document.getElementById('rech'),q=document.getElementById('q'),sg=document.getElementById('sug'),pos={{}};
choix.forEach(function(f,i){{if(f)pos[f.m]=i}});
function nz(x){{return x.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/[^a-z0-9]/g,'')}}
function choisir(k){{c.value=k?pos[k]:0;c.onchange();q.value=k?par[k].nom:'';sg.hidden=true;q.blur()}}
function proposer(){{var v=nz(q.value);sg.innerHTML='';if(!v){{sg.hidden=true;if(c.value!=='0')choisir(null);return}}
var d=[],m=[];ks.forEach(function(k){{var n=nz(par[k].nom);if(n.indexOf(v)===0)d.push(k);else if(n.indexOf(v)>0)m.push(k)}});
d.concat(m).slice(0,8).forEach(function(k){{var b=document.createElement('button');b.type='button';b.dataset.k=k;
var e=document.createElement('span');e.textContent=par[k].n;b.append(par[k].nom,e);sg.appendChild(b)}});
if(!sg.children.length){{var p=document.createElement('p');p.textContent='Aucune marque';sg.appendChild(p)}}sg.hidden=false}}
q.addEventListener('input',proposer);q.addEventListener('focus',function(){{if(q.value)proposer()}});
q.addEventListener('keydown',function(e){{if(e.key==='Enter'){{e.preventDefault();var b=sg.querySelector('button');if(b)choisir(b.dataset.k)}}else if(e.key==='Escape')sg.hidden=true}});
sg.addEventListener('pointerdown',function(e){{e.preventDefault()}});
sg.addEventListener('click',function(e){{var b=e.target.closest('button');if(b)choisir(b.dataset.k)}});
document.addEventListener('click',function(e){{if(!rq.contains(e.target))sg.hidden=true}});
c.addEventListener('change',function(){{var f=choix[+c.value];q.value=f?par[f.m].nom:''}});
rq.hidden=ks.length<2;
// Badges : marques favorites en un clic ; « Choisir » pour en ajouter ou en retirer (gardé sur l'appareil)
var bd=document.getElementById('bdg'),KB='marques_badges',fav=['uniqlo','jott','arket','sezane','bash','bellerose'],edit=false;
try{{var sv=JSON.parse(localStorage.getItem(KB)||'null');if(sv&&sv.map)fav=sv}}catch(e){{}}
function bouton(t,on,fn){{var b=document.createElement('button');b.type='button';b.className='remontrer';b.textContent=t;
if(on){{b.style.background='var(--accent)';b.style.color='var(--bg)'}}b.onclick=fn;bd.appendChild(b)}}
function badges(){{bd.innerHTML='';var cur=choix[+c.value],ck=cur&&!cur.r?cur.m:null;
if(edit){{ks.forEach(function(k){{var z=nz(par[k].nom),on=fav.indexOf(z)>=0;bouton((on?'✓ ':'+ ')+par[k].nom,on,function(){{
if(on)fav.splice(fav.indexOf(z),1);else fav.push(z);try{{localStorage.setItem(KB,JSON.stringify(fav))}}catch(e){{}}badges()}})}});
bouton('Terminé',true,function(){{edit=false;badges()}});return}}
bouton('Toutes',!ck,function(){{choisir(null);badges()}});
ks.filter(function(k){{return fav.indexOf(nz(par[k].nom))>=0}}).forEach(function(k){{bouton(par[k].nom+' ('+par[k].n+')',ck===k,function(){{choisir(ck===k?null:k);badges()}})}});
bouton('✎ Choisir',false,function(){{edit=true;badges()}})}}
c.addEventListener('change',badges);bd.hidden=ks.length<2;badges()}}}})();
(function(){{var K='ventes_masquees',m={{}};
// Croix sur une carte : la vente est masquée sur cet appareil
try{{m=JSON.parse(localStorage.getItem(K)||'{{}}')||{{}}}}catch(e){{}}
function maj(){{document.querySelectorAll('#grid .card').forEach(function(c){{c.classList.toggle('masq',!!m[c.dataset.id])}})}}
function garder(){{try{{localStorage.setItem(K,JSON.stringify(m))}}catch(e){{}}}}
document.getElementById('grid').addEventListener('click',function(e){{var x=e.target.closest('.cache');if(!x)return;e.preventDefault();e.stopPropagation();
m[x.closest('.card').dataset.id]=Date.now();garder();maj()}});
maj()}})();
(function(){{var v=document.getElementById('vue'),im=v.querySelector('img'),bd=v.querySelector('.vph .b'),t=v.querySelector('p'),l=v.querySelector('a'),pv=v.querySelector('.pv'),nx=v.querySelector('.nx'),L=[],k=0;
// Fenêtre photo : flèches ou balayage pour passer aux ventes voisines (celles affichées selon le menu)
function show(){{var x=L[k],c=x.closest('.card');im.src=x.currentSrc||x.src;l.href=x.dataset.u;t.textContent='';
if(c){{var d=c.querySelector('.b.d');bd.textContent=d.textContent;bd.className=d.className;bd.hidden=false;
var b=document.createElement('b');b.textContent=c.querySelector('.b.p').textContent;t.append(b,' · '+c.querySelector('.body .t').textContent+(c.dataset.s?' · taille '+c.dataset.s:''));
if(c.dataset.c){{var co=document.createElement('em');co.className='co';co.textContent=c.dataset.c;t.append(co)}}
if(c.dataset.x){{var e=document.createElement('em');e.textContent=c.dataset.x;t.append(e)}}if(L.length>1)t.append(' · '+(k+1)+' / '+L.length)}}
else{{bd.hidden=true;t.textContent=x.dataset.t}}
pv.hidden=nx.hidden=L.length<2;if(L.length>1){{var n=new Image();n.referrerPolicy='no-referrer';n.src=L[(k+1)%L.length].src}}}}
function va(s){{if(L.length<2)return;k=(k+s+L.length)%L.length;show();if(tic)lancer()}}
// Défilement automatique : ▶ Auto / ❚❚ Pause, vitesse au choix (gardée sur l'appareil)
var ba=v.querySelector('.auto'),bv=v.querySelector('.vit'),VIT=[2,3,5,8],dt=3,tic=null;try{{dt=+localStorage.getItem('diapo_vitesse')||3}}catch(e){{}}
function lancer(){{clearInterval(tic);tic=setInterval(function(){{va(1)}},dt*1000)}}
function arreter(){{clearInterval(tic);tic=null;maj()}}
function maj(){{ba.textContent=tic?'❚❚ Pause':'▶ Auto';ba.classList.toggle('on',!!tic);bv.textContent=dt+' s';ba.hidden=bv.hidden=L.length<2}}
ba.onclick=function(){{if(tic)arreter();else{{lancer();maj()}}}};
bv.onclick=function(){{dt=VIT[(VIT.indexOf(dt)+1)%VIT.length]||3;try{{localStorage.setItem('diapo_vitesse',dt)}}catch(e){{}}if(tic)lancer();maj()}};
l.addEventListener('click',arreter);document.addEventListener('visibilitychange',function(){{if(document.hidden)arreter()}});
function fermer(){{arreter();v.hidden=true;im.removeAttribute('src');document.body.style.overflow=''}}
document.addEventListener('click',function(e){{var x=e.target.closest('img[data-u]');if(!x||v.contains(x))return;e.preventDefault();
var g=x.closest('.card')&&x.closest('.grid');L=g?[].slice.call(g.querySelectorAll('.card:not([hidden]):not(.masq) .ph img[data-u]')):[x];k=Math.max(0,L.indexOf(x));
show();arreter();v.hidden=false;document.body.style.overflow='hidden'}});
pv.onclick=function(){{va(-1)}};nx.onclick=function(){{va(1)}};
v.addEventListener('click',function(e){{if(e.target===v||e.target.classList.contains('x'))fermer()}});
document.addEventListener('keydown',function(e){{if(v.hidden)return;if(e.key==='Escape')fermer();if(e.key==='ArrowRight')va(1);if(e.key==='ArrowLeft')va(-1)}});
var x0=null;v.addEventListener('touchstart',function(e){{x0=e.touches[0].clientX}},{{passive:true}});
v.addEventListener('touchend',function(e){{if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;x0=null;if(Math.abs(dx)>40)va(dx<0?1:-1)}})}})();
(function(){{var t0=Date.now();function recharger(){{location.replace(location.pathname+'?t='+Date.now())}}
document.getElementById('actualiser').onclick=recharger;
var ind=document.getElementById('tirer'),y0=null,dy=0,SEUIL=90;
function poser(d){{ind.style.transform='translate(-50%,'+(Math.min(d,SEUIL+20)-48)+'px)';ind.textContent=d>=SEUIL?'↻ Relâcher pour actualiser':'↓ Tirer pour actualiser'}}
document.addEventListener('touchstart',function(e){{y0=(window.scrollY<=0&&document.getElementById('vue').hidden)?e.touches[0].clientY:null;dy=0}},{{passive:true}});
document.addEventListener('touchmove',function(e){{if(y0===null)return;dy=e.touches[0].clientY-y0;if(dy>0)poser(dy)}},{{passive:true}});
document.addEventListener('touchend',function(){{if(y0===null)return;y0=null;if(dy>=SEUIL){{ind.textContent='↻ Actualisation…';recharger()}}else poser(0)}});
document.addEventListener('visibilitychange',function(){{if(!document.hidden&&Date.now()-t0>120000)recharger()}})}})();</script>{SCRIPT_ETAT}</body></html>"""
    os.makedirs(os.path.dirname(FICHIER_RAPPORT), exist_ok=True)
    with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
        f.write(page)


def ecrire_likes_json(etat):
    """docs/likes.json : les annonces de « Likes rapides », lues par le bouton du navigateur sur Vinted."""
    t0 = etat.get("dernier_passage") or maintenant()
    champs = ("id", "recherche", "titre", "prix", "marque", "taille", "etat_article", "photo", "url", "likes", "publie")
    lst = [{k: a.get(k) for k in champs} for a in (etat.get("likes") or {}).values() if a.get("publie")]
    with open(os.path.join(ICI, "docs", "likes.json"), "w", encoding="utf-8") as f:
        json.dump({"maj": t0, "annonces": lst}, f, ensure_ascii=False, separators=(",", ":"))


def html_likes(etat, t0, zoom):
    """Section « Likes rapides » : nouvelles annonces classées par likes par heure depuis leur mise en ligne."""
    lk = [a for a in (etat.get("likes") or {}).values() if a.get("publie") and (a.get("likes") or 0) >= LIKES_MIN]
    if not lire_recherches_likes() and not lk:
        return ""
    def vitesse(a):
        return a["likes"] / max(0.5, (t0 - a["publie"]) / 3600)
    lk.sort(key=vitesse, reverse=True)
    cartes = []
    for a in lk[:LIKES_AFFICHEES]:
        h = (t0 - a["publie"]) / 3600
        v = vitesse(a)
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)} '
               f'onerror="this.replaceWith(\'Photo expirée\')">') if a["photo"] else "Pas de photo"
        vt = f"{v:.0f}" if v >= 10 else f"{v:.1f}".replace(".", ",")
        cartes.append(
            f'<article class="card" data-age="{h:.2f}" data-m="{html.escape((a["marque"] or "").strip())}" data-k="{html.escape(type_vetement([a["titre"]]) or "autre")}" data-s="{html.escape(a["taille"] or "")}" data-x="♥ {a["likes"]} en {fmt_h(h)}"><div class="ph">{img}<span class="b d{" hot" if v >= 2 else ""}">{vt} ♥/h</span>'
            f'<span class="b p">{fmt_prix(a["prix"])}</span></div><div class="body"><span class="t">{html.escape(a["marque"] or a["recherche"])}</span>'
            f'<span>♥ {a["likes"]} en {fmt_h(h)}</span><span class="m">{html.escape(a["titre"])}</span>'
            f'<span class="m">{html.escape(" · ".join(x for x in (a["taille"], a["etat_article"]) if x))}</span>'
            f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Voir l’annonce ↗</a></div></article>')
    puces = "".join(f'<button type="button" class="remontrer" data-h="{x}">≤ {x} h</button>' for x in (1, 3, 6, 12, 24))
    nb = {}
    for a in lk[:LIKES_AFFICHEES]:
        mq = (a["marque"] or "").strip()
        if mq:
            nb[mq] = nb.get(mq, 0) + 1
    if len(nb) > 1:
        puces += ('<select id="lkm" aria-label="Marque"><option value="">Toutes les marques</option>'
                  + "".join(f'<option value="{html.escape(m)}">{html.escape(m)} ({n})</option>' for m, n in sorted(nb.items(), key=lambda x: (-x[1], x[0].lower())))
                  + '</select>')
    nk = {}
    for a in lk[:LIKES_AFFICHEES]:
        k = type_vetement([a["titre"]]) or "autre"
        nk[k] = nk.get(k, 0) + 1
    if len(nk) > 1:
        puces += ('<select id="lkc" aria-label="Catégorie"><option value="">Toutes les catégories</option>'
                  + "".join(f'<option value="{html.escape(k)}">{html.escape(k.capitalize())} ({n})</option>' for k, n in sorted(nk.items(), key=lambda x: (x[0] == "autre", -x[1], x[0])))
                  + '</select>')
    return (
        f"""<section><details class="repli"><summary><h2>Likes rapides</h2></summary><div class="filtres" id="lkf">{puces}</div>"""
        f"""<div class="grid" id="lkg">{''.join(cartes) or '<p class="sub">Pas encore d’annonce : le premier relevé arrive au prochain passage.</p>'}</div>"""
        f"""<p class="sub" id="lkn"></p><p class="sub">Nouvelles annonces des recherches de <code>recherches_likes.txt</code>, particuliers seulement (sans les sliders « Voir le vendeur », les vendeurs Pro et les annonces boostées ou remontées), avec au moins {LIKES_MIN} likes. """
        f"""Classées par likes par heure depuis la mise en ligne (rouge : 2 ♥/h ou plus). Likes relevés à chaque passage.</p></details></section>"""
        """<script>(function(){var f=document.getElementById('lkf'),g=document.getElementById('lkg'),n=document.getElementById('lkn'),sm=document.getElementById('lkm'),K='likes_fenetre',KM='likes_marque',h=6,mq='';
try{h=+localStorage.getItem(K)||6;mq=localStorage.getItem(KM)||''}catch(e){}
if(sm){if(![].some.call(sm.options,function(o){return o.value===mq}))mq='';sm.value=mq;sm.onchange=function(){mq=sm.value;try{localStorage.setItem(KM,mq)}catch(e){}maj()}}else mq='';
var sc=document.getElementById('lkc'),KC='likes_categorie',ct='';try{ct=localStorage.getItem(KC)||''}catch(e){}
if(sc){if(![].some.call(sc.options,function(o){return o.value===ct}))ct='';sc.value=ct;sc.onchange=function(){ct=sc.value;try{localStorage.setItem(KC,ct)}catch(e){}maj()}}else ct='';
function maj(){var c=0;g.querySelectorAll('.card').forEach(function(e){var x=+e.dataset.age>h||(mq&&e.dataset.m!==mq)||(ct&&e.dataset.k!==ct);e.hidden=x;if(!x)c++});
f.querySelectorAll('button').forEach(function(b){var on=+b.dataset.h===h;b.style.background=on?'var(--accent)':'';b.style.color=on?'var(--bg)':''});
n.textContent=c?'':'Aucune annonce'+(ct?' ('+ct+')':'')+(mq?' '+mq:'')+' mise en ligne depuis moins de '+h+' h avec assez de likes.'}
f.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;h=+b.dataset.h;try{localStorage.setItem(K,h)}catch(e){}maj()});maj()})();</script>""")


def slug(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "recherche"


def ecrire_export(etat):
    """Toutes les ventes gardées, en texte brut : un sommaire, puis des pages de 120 ventes par recherche."""
    vendus = sorted([a for a in etat["annonces"].values() if a["statut"] == "vendu"], key=lambda a: a["vendu_vu"], reverse=True)
    par = {}
    for a in vendus:
        par.setdefault(a["recherche"], []).append(a)
    os.makedirs(DOSSIER_EXPORT, exist_ok=True)
    for f in os.listdir(DOSSIER_EXPORT):
        os.remove(os.path.join(DOSSIER_EXPORT, f))
    paris = ZoneInfo("Europe/Paris")
    sommaire = ["Ventes par recherche (délai | prix | taille | couleur | titre | vendue le | likes | annonce | photo)", ""]
    for nom, lst in sorted(par.items()):
        pages = [lst[k:k + EXPORT_LIGNES] for k in range(0, len(lst), EXPORT_LIGNES)]
        fichiers = []
        for n, morceau in enumerate(pages, 1):
            fichier = f"{slug(nom)}-{n}.txt"
            lignes = [f"{nom} · page {n}/{len(pages)} · {len(lst)} ventes"]
            for a in morceau:
                quand = datetime.fromtimestamp(a["vendu_vu"], paris).strftime("%d/%m %H:%M")
                lignes.append(" | ".join([fmt_h(heures(a.get("publie"), a["vendu_vu"])), fmt_prix(a["prix"]), a["taille"] or "?", a.get("couleur") or "?",
                                          " ".join(a["titre"].split()), quand, str(a.get("likes") or 0), a["url"].split("?")[0], a["photo"] or "-"]))
            with open(os.path.join(DOSSIER_EXPORT, fichier), "w", encoding="utf-8") as f:
                f.write("\n".join(lignes) + "\n")
            fichiers.append(fichier)
        sommaire.append(f"{nom} ({len(lst)}) : {' '.join(fichiers)}")
    with open(os.path.join(DOSSIER_EXPORT, "index.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sommaire) + "\n")


# ---------------------------------------------------------------- base des meilleures ventes

MODELES = []  # modèles sûrs du dernier passage (voir modeles_surs)

def lire_base():
    try:
        with open(FICHIER_BASE, encoding="utf-8") as f:
            b = json.load(f)
        b.setdefault("ventes", {})
        return b
    except (OSError, ValueError):
        return {"ventes": {}, "enregistre": 0}


def semaine_de(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("Europe/Paris")).strftime("%G-%V")


def photo_reduite(v):
    """Garde la photo en petit dans le dépôt : les liens de photo Vinted finissent par expirer."""
    nom = f"{v['id']}.jpg"
    chemin = os.path.join(DOSSIER_BASE, "photos", nom)
    if os.path.exists(chemin):
        return nom
    if not v.get("photo_url"):
        return ""
    try:
        from PIL import Image
        r = session.get(v["photo_url"], timeout=20)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((300, 400))
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        im.save(chemin, "JPEG", quality=72, optimize=True, progressive=True)
        return nom
    except Exception as e:
        log(f"   photo {v['id']} : {e}")
        return ""


def maj_base(etat, t0):
    """Ajoute à la base les meilleures ventes du moment et garde, par recherche et par semaine, les plus rapides."""
    base = lire_base()
    avant = json.dumps(base["ventes"], sort_keys=True)
    ventes = base["ventes"]
    vendus = [a for a in etat["annonces"].values()
              if a["statut"] == "vendu" and a.get("publie") and a.get("vendu_vu") and a["prix"] is not None]
    par = {}
    for a in vendus:
        par.setdefault(a["recherche"], []).append(a["prix"])
    med = {k: statistics.median(v) for k, v in par.items()}
    for a in vendus:
        h = heures(a["publie"], a["vendu_vu"])
        if h is None or h > BASE_MAX_H or a["prix"] < med[a["recherche"]]:
            continue
        v = ventes.get(a["id"])
        if v:
            if a.get("couleur"):
                v["couleur"] = a["couleur"]
            continue
        ventes[a["id"]] = {
            "id": a["id"], "recherche": a["recherche"], "marque": (a["marque"] or "").strip(), "titre": " ".join(a["titre"].split()),
            "prix": a["prix"], "taille": a["taille"] or "", "couleur": a.get("couleur") or "", "etat": a["etat_article"] or "",
            "likes": a.get("likes") or 0, "delai_h": round(h, 2), "publie": a["publie"], "vendu": a["vendu_vu"],
            "semaine": semaine_de(a["vendu_vu"]), "median": med[a["recherche"]],
            "url": a["url"].split("?")[0], "photo_url": a["photo"] or "", "ph": "",
        }
    # Par recherche et par semaine : les plus rapides, puis les plus chères
    groupes = {}
    for v in ventes.values():
        groupes.setdefault((v["recherche"], v["semaine"]), []).append(v)
    garde = {}
    for lst in groupes.values():
        lst.sort(key=lambda v: (v["delai_h"], -v["prix"], -(v.get("likes") or 0)))
        for v in lst[:BASE_PAR_SEMAINE]:
            garde[v["id"]] = v
    for v in garde.values():
        if not v.get("ph"):
            v["ph"] = photo_reduite(v)
    # Modèles sûrs, d'après l'historique des ventes rapides ; leur photo est gardée comme celles de la base
    global MODELES
    histo, histo_change = maj_historique(etat, t0)
    MODELES = modeles_surs(histo)
    for m in MODELES:
        v0 = m["ventes"][0]
        v0["ph"] = (garde[v0["id"]].get("ph") if v0["id"] in garde else "") or photo_reduite({"id": v0["id"], "photo_url": v0["f"]})
    log(f"Modèles sûrs : {len(MODELES)}")
    dossier_ph = os.path.join(DOSSIER_BASE, "photos")
    if os.path.isdir(dossier_ph):
        gardees = {v["ph"] for v in garde.values() if v.get("ph")} | {m["ventes"][0]["ph"] for m in MODELES if m["ventes"][0].get("ph")}
        for f in os.listdir(dossier_ph):
            if f not in gardees:
                os.remove(os.path.join(dossier_ph, f))
    base["ventes"] = garde
    if json.dumps(garde, sort_keys=True) != avant or histo_change:
        if t0 - (base.get("enregistre") or 0) >= BASE_ENREGISTRER_H * 3600:
            base["enregistre"] = t0
            open(FICHIER_BASE_A_ENREGISTRER, "w").close()  # la tâche GitHub enregistre la base dans le dépôt
        with open(FICHIER_BASE, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=0, sort_keys=True)
    log(f"Base des meilleures ventes : {len(garde)} ventes")
    ecrire_page_base(garde, t0)


CSS_BASE = """.outils{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.outils input,.outils select{flex:1 1 200px;min-width:0;font:600 16px system-ui;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 12px}
.outils input{flex-basis:100%}.compte{color:var(--muted);font-size:14px;margin:0 0 10px}.plus{display:block;margin:16px auto 0;font:600 15px system-ui;color:var(--accent);background:var(--accent-soft);border:0;border-radius:99px;padding:10px 20px;cursor:pointer}
.ph a{display:contents}"""


def ecrire_page_base(garde, t0):
    paris = ZoneInfo("Europe/Paris")
    lst = sorted(garde.values(), key=lambda v: v["vendu"], reverse=True)
    donnees = [{
        "i": v["id"], "r": v["recherche"], "m": v["marque"], "t": v["titre"], "p": v["prix"], "s": v["taille"], "c": v.get("couleur") or "",
        "e": v["etat"], "l": v.get("likes") or 0, "h": fmt_h(v["delai_h"]), "hn": v["delai_h"], "v": v["vendu"],
        "d": datetime.fromtimestamp(v["vendu"], paris).strftime("%d/%m/%Y"), "px": fmt_prix(v["prix"]),
        "a": fmt_prix(round(prix_achat_max(v["prix"]))), "u": v["url"], "f": ("photos/" + v["ph"]) if v.get("ph") else "", "fu": v.get("photo_url") or "",
    } for v in lst]
    # Export tableur
    os.makedirs(DOSSIER_BASE, exist_ok=True)
    with open(os.path.join(DOSSIER_BASE, "meilleures-ventes.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Vendue le", "Recherche", "Marque", "Titre", "Prix", "Délai", "Taille", "Couleur", "État", "Likes", "Achat max", "Annonce"])
        for d in donnees:
            w.writerow([d["d"], d["r"], d["m"], d["t"], d["px"], d["h"], d["s"], d["c"], d["e"], d["l"], d["a"], d["u"]])
    maj = datetime.fromtimestamp(t0, paris).strftime("%d/%m/%Y à %H:%M")
    page = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>Meilleures ventes</title>
<link rel="manifest" href="../manifest.json"><link rel="apple-touch-icon" href="../icone-180.png"><meta name="theme-color" content="#2f5d50">
<style>__CSS__</style></head><body><div class="wrap">
<header><div class="haut"><h1>Meilleures ventes</h1><div class="boutons"><a href="../">← Ventes</a><a href="meilleures-ventes.csv" download>Tableur</a></div></div>
<p class="sub">Gardées pour de bon : chaque semaine, les __N__ ventes les plus rapides de chaque recherche, parties en __H__ h au plus et au prix médian de leur recherche ou plus. Mis à jour le __MAJ__.</p></header>
<section><details class="repli" open><summary><h2>Modèles sûrs (__NM__)</h2></summary>__MODELES__<p class="sub">__TM__</p></details></section>
<section><h2>Toutes les meilleures ventes</h2><div class="outils"><input type="search" id="q" placeholder="Chercher (modèle, marque, couleur, taille…)" aria-label="Chercher" autocomplete="off" autocorrect="off" spellcheck="false" enterkeyhint="search">
<select id="m" aria-label="Marque"></select><select id="r" aria-label="Recherche"></select>
<select id="o" aria-label="Tri"><option value="v">Plus récentes</option><option value="h">Plus rapides</option><option value="p">Plus chères</option><option value="l">Plus aimées</option></select></div>
<p class="compte" id="n"></p><div class="grid" id="grid"></div><button type="button" class="plus" id="plus" hidden>Afficher plus</button></section></div>
<script>var D=__DATA__;
(function(){var g=document.getElementById('grid'),q=document.getElementById('q'),m=document.getElementById('m'),r=document.getElementById('r'),o=document.getElementById('o'),n=document.getElementById('n'),plus=document.getElementById('plus'),lim=120;
function nz(x){return (x||'').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')}
function remplir(s,tous,cle){var c={};D.forEach(function(d){if(d[cle])c[d[cle]]=(c[d[cle]]||0)+1});
var o0=document.createElement('option');o0.value='';o0.textContent=tous+' ('+D.length+')';s.appendChild(o0);
Object.keys(c).sort(function(a,b){return a.localeCompare(b,'fr')}).forEach(function(k){var e=document.createElement('option');e.value=k;e.textContent=k+' ('+c[k]+')';s.appendChild(e)})}
remplir(m,'Toutes les marques','m');remplir(r,'Toutes les recherches','r');
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function carte(d){var a=el('article','card'),ph=el('div','ph'),lien=el('a');lien.href=d.u;lien.target='_blank';lien.rel='noopener';
var src=d.f||d.fu;if(src){var im=el('img');im.loading='lazy';im.referrerPolicy='no-referrer';im.alt='';im.src=src;
im.onerror=function(){if(d.f&&d.fu&&im.src.indexOf(d.f)>=0){im.src=d.fu}else{im.replaceWith('Pas de photo')}};lien.appendChild(im)}else lien.textContent='Pas de photo';
ph.appendChild(lien);var b=el('span','b d'+(d.hn<=2?' hot':''),'≤ '+d.h);ph.appendChild(b);ph.appendChild(el('span','b p',d.px));
var bd=el('div','body');bd.appendChild(el('span','m',d.r));bd.appendChild(el('span','t',d.t));
bd.appendChild(el('span','m',[d.m,d.s,d.c,d.e].filter(Boolean).join(' · ')+(d.l?' · ♥ '+d.l:'')));
bd.appendChild(el('span','m','Vendue le '+d.d));bd.appendChild(el('span','achat','Achat max ≤ '+d.a));
var v=el('a',null,'Voir l’annonce ↗');v.href=d.u;v.target='_blank';v.rel='noopener';bd.appendChild(v);a.append(ph,bd);return a}
function rendu(){var t=nz(q.value).split(/\\s+/).filter(Boolean),L=D.filter(function(d){if(m.value&&d.m!==m.value)return false;if(r.value&&d.r!==r.value)return false;
var txt=nz([d.t,d.m,d.r,d.c,d.s,d.e].join(' '));return t.every(function(w){return txt.indexOf(w)>=0})});
var k=o.value;L.sort(function(a,b){return k==='h'?a.hn-b.hn||b.p-a.p:k==='p'?b.p-a.p:k==='l'?b.l-a.l:b.v-a.v});
g.innerHTML='';L.slice(0,lim).forEach(function(d){g.appendChild(carte(d))});
n.textContent=L.length+' vente'+(L.length>1?'s':'')+(L.length?'':' : rien ne correspond');plus.hidden=L.length<=lim}
[q,m,r,o].forEach(function(e){e.addEventListener(e===q?'input':'change',function(){lim=120;rendu()})});
plus.onclick=function(){lim+=120;rendu();garder()};
// Garde la recherche, les menus et le défilement quand on revient sur la page
var K='meilleures_etat',pret=false;try{history.scrollRestoration='manual'}catch(x){}
function garder(){if(!pret)return;try{localStorage.setItem(K,JSON.stringify({t:Date.now(),q:q.value,m:m.value,r:r.value,o:o.value,lim:lim,y:window.scrollY}))}catch(x){}}
var e=null;try{e=JSON.parse(localStorage.getItem(K)||'null')}catch(x){}
function ok(s,v){return [].some.call(s.options,function(x){return x.value===v})}
if(e){q.value=e.q||'';if(ok(m,e.m))m.value=e.m;if(ok(r,e.r))r.value=e.r;if(ok(o,e.o))o.value=e.o;lim=Math.max(120,+e.lim||120)}
rendu();if(e&&Date.now()-e.t<6*3600e3){var y=e.y||0;window.scrollTo(0,y);setTimeout(function(){window.scrollTo(0,y)},400)}
pret=true;var tm=null;window.addEventListener('scroll',function(){clearTimeout(tm);tm=setTimeout(garder,250)},{passive:true});
[q,m,r,o].forEach(function(x){x.addEventListener(x===q?'input':'change',garder)});
document.addEventListener('visibilitychange',function(){if(document.hidden)garder()});window.addEventListener('pagehide',garder)})();</script></body></html>"""
    page = (page.replace("__CSS__", CSS + "\n" + CSS_BASE).replace("__N__", str(BASE_PAR_SEMAINE)).replace("__H__", str(BASE_MAX_H))
            .replace("__MAJ__", maj).replace("__NM__", str(len(MODELES))).replace("__MODELES__", html_modeles(MODELES, "photos/")).replace("__TM__", html.escape(texte_modeles())).replace("__DATA__", json.dumps(donnees, ensure_ascii=False).replace("</", "<\\/")))
    with open(os.path.join(DOSSIER_BASE, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- modèles sûrs

# Mots qui ne désignent pas un modèle : vêtements, couleurs, matières, tailles, états (plusieurs langues)
MOTS_COMMUNS = set("""
gilet gilets pull pulls pullover pullovers sweat sweatshirt hoodie hoody veste vestes vest vestje weste westen jas jasje jacket jacke jackets giacca giubbotto chaqueta chaleco
manteau manteaux mantel coat cappotto abrigo jassen parka parkas doudoune doudounes puffer down steppjacke daunenjacke piumino plumifero blouson bomber bomberjacke
trench imper impermeable caban duffle cardigan cardigans strickjacke jersey jumper sweater maglione maglia trui pulli winterpulli robe robes dress kleid vestido jurk
jupe skirt rock gonna falda rok pantalon pantalons pants hose jean jeans denim short shorts chemise chemisier blouse bluse camisa camicia shirt overhemd body top tee
tshirt t-shirt debardeur polo polaire polaires fleece fleecejacke sherpa teddy peluche moumoute fourrure fausse faux fur borg combinaison ensemble tailleur costume blazer
sans manche manches mouwloos sleeveless zip zippe zippee zippe zipper half quart capuche hood capuchon col montant roule rond boutons bouton noeuds noeud brode brodee
laine wool wolle lana wol cachemire cashmere kaschmir mohair alpaga alpaca yack yak merinos merino coton cotton baumwolle lin linen leinen soie silk cuir leather leder
lederjacke daim suede wildleder velours corduroy tweed maille tricot knit knitted gebreid ajoure ajouree cotele bouclette boucle matelasse matelassee quilted doublee
noir noire black schwarz schwarzer nero negro zwart blanc blanche white weiss weiß bianco blanco wit ecru creme beige camel marron brown braun brun chocolat taupe
gris grise grey gray grau grigio grijs anthracite bleu bleue blue blau blu azul blauw marine navy ciel rouge red rot rosso rood rose pink rosa roze vert verte green
grun gruen verde groen kaki khaki olive jaune yellow orange violet lila lilas mauve parme bordeaux brique moutarde multicolore chine chinee raye rayee rayures carreaux
taille size talla tg grosse gr maat xxs xs xl xxl femme femmes homme hommes women woman men damen herren dames donna mujer unisexe enfant fille garcon
neuf neuve etiquette etat tres bon bonne excellent parfait parfaite comme new top zustand nauwelijks gedragen worn porte portee peu jamais vintage oversize oversized
ample long longue court courte mi-long cintre droit fluide leger legere chaud chaude epais hiver winter ete printemps automne collection modele model style classic
classique basique magnifique superbe joli jolie belle beau avec pour sur the and von mit und van een les des une pas tres ou et en de du la le
logo motif matiere dark light bright lot pieces piece small medium large petit grand regular slim fit loose crop cropped
""".split())


def _nz(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


def mots_modele(titre, exclus):
    """Mots du titre qui peuvent être un nom de modèle (écrits avec une majuscule)."""
    out = []
    for m in re.findall(r"[A-Za-zÀ-ÿ]{3,}", titre or ""):
        k = _nz(m)
        if k in MOTS_COMMUNS or k in exclus or not m[0].isupper():
            continue
        out.append(cle_modele(k))
    return out


def cle_modele(k):
    """Rapproche les orthographes d'un même nom (Émile, Emil, Emile ; Hooktown, Hoktown)."""
    k = re.sub(r"(.)\1+", r"\1", k)
    return k[:-1] if len(k) > 4 and k.endswith("e") else k


def type_vetement(titres):
    """Le type de vêtement le plus cité (gilet, pull…), pour nommer le modèle."""
    noms = {"gilet": "gilet", "cardigan": "gilet", "strickjacke": "gilet", "pull": "pull", "pullover": "pull", "sweat": "sweat",
            "veste": "veste", "jacket": "veste", "jacke": "veste", "giacca": "veste", "manteau": "manteau", "mantel": "manteau",
            "coat": "manteau", "cappotto": "manteau", "parka": "parka", "doudoune": "doudoune", "puffer": "doudoune", "trench": "trench",
            "blazer": "blazer", "robe": "robe", "jupe": "jupe", "pantalon": "pantalon", "chemise": "chemise", "blouse": "blouse",
            "polaire": "polaire", "fleece": "polaire", "weste": "gilet sans manches", "vest": "gilet sans manches",
            "jean": "jean", "jeans": "jean", "top": "top", "debardeur": "top", "short": "short", "combinaison": "combinaison",
            "bottes": "chaussures", "bottines": "chaussures", "boots": "chaussures", "baskets": "chaussures", "sneakers": "chaussures",
            "escarpins": "chaussures", "sandales": "chaussures", "mocassins": "chaussures", "sac": "sac", "pochette": "sac",
            "echarpe": "accessoire", "bonnet": "accessoire", "ceinture": "accessoire", "foulard": "accessoire"}
    c = {}
    for t in titres:
        for m in re.findall(r"[a-z]+", _nz(t)):
            if m in noms:
                c[noms[m]] = c.get(noms[m], 0) + 1
                break
    return max(c, key=c.get) if c else ""


def maj_historique(etat, t0):
    """Toutes les ventes rapides des HISTO_J derniers jours (sans photo enregistrée), pour reconnaître les modèles sûrs."""
    try:
        with open(FICHIER_HISTO, encoding="utf-8") as f:
            histo = json.load(f)
    except (OSError, ValueError):
        histo = {}
    avant = len(histo), max((v["v"] for v in histo.values()), default=0)
    for a in etat["annonces"].values():
        if a["statut"] != "vendu" or not a.get("publie") or not a.get("vendu_vu") or a["prix"] is None or a["id"] in histo:
            continue
        h = heures(a["publie"], a["vendu_vu"])
        if h is None or h > BASE_MAX_H:
            continue
        histo[a["id"]] = {"r": a["recherche"], "m": (a["marque"] or "").strip(), "t": " ".join(a["titre"].split()), "p": a["prix"],
                          "h": round(h, 2), "v": a["vendu_vu"], "s": a["taille"] or "", "u": a["url"].split("?")[0], "f": a["photo"] or ""}
    limite = t0 - HISTO_J * 86400
    histo = {i: v for i, v in histo.items() if v["v"] >= limite}
    change = (len(histo), max((v["v"] for v in histo.values()), default=0)) != avant
    if change:
        with open(FICHIER_HISTO, "w", encoding="utf-8") as f:
            json.dump(histo, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return histo, change


def modeles_surs(histo):
    """Modèles vendus au moins MODELE_MIN_VENTES fois en BASE_MAX_H h au plus, à un prix régulier."""
    par_marque = {}
    for i, v in histo.items():
        par_marque.setdefault(v["m"] or v["r"], []).append(dict(v, id=i))
    out = []
    for marque, lst in par_marque.items():
        exclus = set(re.findall(r"[a-z]+", _nz(marque + " " + " ".join({v["r"] for v in lst}))))
        compte = {}
        for v in lst:
            v["_mots"] = list(dict.fromkeys(mots_modele(v["t"], exclus)))
            for k in v["_mots"]:
                compte[k] = compte.get(k, 0) + 1
        groupes = {}
        for v in lst:
            if v["_mots"]:
                k = max(v["_mots"], key=lambda k: (compte[k], k))
                groupes.setdefault(k, []).append(v)
        for k, g in groupes.items():
            if len(g) < MODELE_MIN_VENTES:
                continue
            px = sorted(v["p"] for v in g)
            med = statistics.median(px)
            reguliers = sum(1 for p in px if abs(p - med) <= MODELE_ECART * med)
            if reguliers < 2 / 3 * len(g):
                continue
            g.sort(key=lambda v: v["v"], reverse=True)
            formes = [m for v in g for m in re.findall(r"[A-Za-zÀ-ÿ]{3,}", v["t"]) if m[0].isupper() and cle_modele(_nz(m)) == k]
            nom = max(set(formes), key=formes.count) if formes else k.title()
            out.append({"marque": marque, "nom": nom, "type": type_vetement(v["t"] for v in g), "n": len(g), "median": med,
                        "min": px[0], "max": px[-1], "achat": prix_achat_max(med), "delai": statistics.median(v["h"] for v in g),
                        "dernier": g[0]["v"], "ventes": g[:4]})
    out.sort(key=lambda m: (m["n"], -m["delai"], m["median"]), reverse=True)
    return out


def html_modeles(modeles, chemin_photos):
    """Cartes « Modèles sûrs » : photo, revente médiane, achat max, nombre de ventes rapides, délai médian."""
    paris = ZoneInfo("Europe/Paris")
    cartes = []
    for m in modeles:
        v0 = m["ventes"][0]
        src = (chemin_photos + v0["ph"]) if v0.get("ph") else v0["f"]
        fu = f' data-fu="{html.escape(v0["f"])}"' if v0.get("ph") and v0["f"] else ""
        cherche = f"{BASE or 'https://www.vinted.fr'}/catalog?" + urlencode({"search_text": f"{m['marque']} {m['nom']}", "order": "newest_first"})
        nom = f"{m['type'].capitalize() + ' ' if m['type'] else ''}{m['nom']}"
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(src)}"{fu} data-u="{html.escape(cherche)}" data-t="{html.escape(m["marque"] + " " + nom)}" '
               'onerror="if(this.dataset.fu&&this.src!==this.dataset.fu){this.src=this.dataset.fu}else{this.replaceWith(\'Photo expirée\')}">') if src else "Pas de photo"
        vig = "".join(f'<a href="{html.escape(v["u"])}" target="_blank" rel="noopener" title="{html.escape(v["t"])} · {fmt_prix(v["p"])} · ≤ {fmt_h(v["h"])}">{fmt_prix(v["p"])}</a>'
                      for v in m["ventes"])
        cartes.append(
            f'<article class="card" data-m="{html.escape(m["marque"])}" data-x="achat max ≤ {fmt_prix(round(m["achat"]))}"><div class="ph"><a href="{html.escape(cherche)}" target="_blank" rel="noopener" style="display:contents">{img}</a>'
            f'<span class="b d hot">{m["n"]} ventes ≤ {BASE_MAX_H} h</span><span class="b p">{fmt_prix(m["median"])}</span></div>'
            f'<div class="body"><span class="m">{html.escape(m["marque"])}</span><span class="t">{html.escape(nom)}</span>'
            f'<span class="achat">Achat max ≤ {fmt_prix(round(m["achat"]))}</span>'
            f'<span class="m">Revente {fmt_prix(m["min"])} à {fmt_prix(m["max"])} · délai médian {fmt_h(m["delai"])}</span>'
            f'<span class="m">Dernière vente le {datetime.fromtimestamp(m["dernier"], paris).strftime("%d/%m")}</span>'
            f'<span class="ventes">{vig}</span>'
            f'<a href="{html.escape(cherche)}" target="_blank" rel="noopener">Chercher sur Vinted ↗</a></div></article>')
    if not cartes:
        return f'<p class="sub">Pas encore de modèle sûr : il en faut un vendu au moins {MODELE_MIN_VENTES} fois en {BASE_MAX_H} h au plus, à un prix régulier.</p>'
    return f'<div class="grid">{"".join(cartes)}</div>'


def texte_modeles():
    return (f"Modèles vendus au moins {MODELE_MIN_VENTES} fois en {BASE_MAX_H} h au plus sur les {HISTO_J} derniers jours, "
            f"avec au moins 2 ventes sur 3 à ± {MODELE_ECART * 100:.0f} % du prix médian. Le modèle est reconnu par son nom dans le titre "
            f"(ex. Othello, Hoktown). Achat max : {ACHAT_PART * 100:.0f} % du prix médian, avec au moins {ACHAT_MARGE} € de marge. "
            f"Les montants sous la carte sont les dernières ventes.")


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
