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
FICHIER_ETAT = os.path.join(ICI, "etat.json")
FICHIER_ETAT_ANCIEN = os.path.join(ICI, "etat_ancien.json")  # mémoire d'avant le retrait de recherches, relue une seule fois
RESTAURER = ["Ba&sh sweat", "Sézane sweat", "Sézane trench"]  # recherches retirées dont on récupère les ventes passées
FICHIER_RAPPORT = os.path.join(ICI, "docs", "index.html")
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
MODELE_MIN = 3              # annonces minimum (vendues ou en vente) pour qu'un mot du titre devienne un modèle
NOUVEAU_H = 72              # badge « Nouveau » : modèle apparu depuis moins de X heures…
NOUVEAU_LIKES = 5           # … qui a déjà une vente ou une annonce avec au moins X likes
CHAUFFE_N = 10              # annonces en vente affichées dans « Ça chauffe »
CHAUFFE_MIN_LIKES = 3       # likes minimum pour y entrer
PROFIL_MIN = 5              # annonces minimum pour juger une couleur, une taille ou une coupe d'une marque
LISTE_N = 10                # « Ma liste » : modèles gardés au maximum…
LISTE_MIN_RAPIDES = 3       # … qui ont au moins X ventes en moins de 24 h
COPIE_MARQUE = False        # True : copie de chaque vente sur le sujet de sa marque (sujet-marque) en plus du sujet principal
RESUME_JOUR, RESUME_HEURE = 0, 8  # résumé de la semaine le lundi (0) à partir de 8 h

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


# ---------------------------------------------------------------- modèles

# Mots ignorés pour repérer les modèles dans les titres (sans accents, au singulier)
MOTS_VIDES = set("""
femme homme enfant fille garcon mixte unisexe adulte ado taille size tres bon bonne etat neuf neuve excellent parfait impeccable
comme jamais porte peu etiquette vend vends vendu modele model marque original authentique collection saison
avec sans pour dans les des une sur par aux est tout toute the and with for und mit
hiver ete automne printemps chaud chaude leger legere fine long longue court courte mid oversize ample cintre cintree droit droite
capuche zip zippe zippee col rond manche longues courtes poche bouton doublure double doublee fourrure fourre fourree
duvet plume moumoute amovible reversible matelasse matelassee
noir noire blanc blanche bleu marine beige gris grise rouge vert verte kaki rose camel marron ecru creme jaune orange violet
bordeaux taupe chocolat anthracite black white navy blue grey gray green red brown multicolore fonce clair aubergine moutarde
dore argent brillant mat chine raye fleuri imprime uni
xxs xxl xxxl petit grand grande large slim regular oversized vintage neuwertig paket
maat talla gro women woman men damen herren lady ladie sleeveless smanicato senza manica mangas hiver winter
pink rosa burgundy grau hellgrau grijs cream dark light fluffy sand lavande fushia fuchsia vieux meliert heather iguane
""".split())
MATIERES = {"laine", "cachemire", "coton", "lin", "soie", "cuir", "daim", "velours", "mohair", "alpaga", "merinos", "jean", "denim", "polyester", "sherpa", "teddy"}
TYPES = {
    "doudoune": "Doudoune", "veste": "Veste", "manteau": "Manteau", "parka": "Parka", "blouson": "Blouson", "pull": "Pull",
    "gilet": "Gilet", "cardigan": "Cardigan", "sweat": "Sweat", "sweatshirt": "Sweat", "hoodie": "Sweat", "polaire": "Polaire",
    "fleece": "Polaire", "chemise": "Chemise", "blouse": "Blouse", "robe": "Robe", "jupe": "Jupe", "pantalon": "Pantalon",
    "short": "Short", "trench": "Trench", "blazer": "Blazer", "top": "Top", "tshirt": "T-shirt", "debardeur": "Débardeur",
    "combinaison": "Combinaison", "botte": "Bottes", "bottine": "Bottines", "sac": "Sac", "basket": "Baskets", "chaussure": "Chaussures",
    "impermeable": "Imperméable", "ciré": "Ciré", "cire": "Ciré", "coupe": "Coupe-vent", "jacket": "Veste", "coat": "Manteau",
    "doudounette": "Doudoune", "vest": "Gilet", "weste": "Gilet", "bodywarmer": "Gilet", "chaleco": "Gilet",
    "jacke": "Veste", "jas": "Veste", "jasje": "Veste", "fleecejacke": "Polaire", "fleecejas": "Polaire", "mantel": "Manteau",
    "sweater": "Pull", "pullover": "Pull", "trui": "Pull", "strickjacke": "Cardigan", "jogger": "Jogging", "daunenjacke": "Doudoune", "bomber": "Blouson", "kimono": "Kimono", "chemisier": "Chemisier", "jogging": "Jogging",
}


def _sans_accent(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


def mots_titre(titre):
    """Mots utiles d'un titre : (clé sans accent au singulier, mot d'origine)."""
    out = []
    for w in re.findall(r"[^\W\d_]+", titre or ""):
        k = _sans_accent(w)
        if len(k) > 4 and k.endswith("s"):
            k = k[:-1]
        if len(k) >= 3:
            out.append((k, w))
    return out


COULEURS = {
    "Noir": "noir noire black schwarz zwart negro nero", "Blanc / écru": "blanc blanche white ecru creme cream weis wit offwhite",
    "Bleu": "bleu marine navy blue blau blauw azul", "Gris": "gris grise grey gray anthracite grau grijs",
    "Beige / camel": "beige camel sand taupe sable", "Vert / kaki": "vert verte kaki khaki green olive grun groen",
    "Rose": "rose pink rosa", "Rouge / bordeaux": "rouge red bordeaux burgundy rot rood", "Marron": "marron brown chocolat braun bruin cognac",
    "Jaune / moutarde": "jaune moutarde yellow mustard gelb", "Violet": "violet aubergine lilas lavande purple",
}
COULEUR_DE = {m: c for c, mots in COULEURS.items() for m in mots.split()}
FEMININS = {"Doudoune", "Veste", "Parka", "Chemise", "Blouse", "Robe", "Jupe", "Polaire", "Combinaison", "Doudounette"}


def coupe_titre(titre):
    t = " " + re.sub(r"[^a-z0-9/]+", " ", _sans_accent(titre or "")) + " "
    if any(m in t for m in (" sans manche", " sleeveless", " bodywarmer", " smanicato", " sin manga")):
        return "sans manches"
    if any(m in t for m in (" mi long", " 3/4 ", " midi ")):
        return "mi-long"
    if any(m in t for m in (" long ", " longue ", " lang ", " maxi ")):
        return "long"
    if any(m in t for m in (" court ", " courte ", " crop", " cropped ", " kurz ")):
        return "court"
    return None


def couleur_titre(titre):
    for k, _ in mots_titre(titre):
        if k in COULEUR_DE:
            return COULEUR_DE[k]
    return None


def taille_courte(a):
    t = (a.get("taille") or "").split("/")[0].strip()
    return t.upper() if t else None


def libelle_coupe(typ, coupe):
    if not coupe or coupe == "sans manches":
        return coupe or ""
    return coupe + (("ue" if coupe.endswith("long") else "e") if typ in FEMININS else "")


def cle_marque(a):
    return (a.get("marque") or "").strip() or a["recherche"]


def modeles(etat, t0):
    """Regroupe toutes les annonces gardées par marque puis par modèle (mot qui revient dans les titres).
    Renvoie (groupes, groupe de chaque annonce)."""
    par = {}
    for a in etat["annonces"].values():
        if a["statut"] != "supprime":
            par.setdefault(cle_marque(a), []).append(a)
    groupes, de = {}, {}
    for marque, lst in par.items():
        exclus = {k for k, _ in mots_titre(marque)} | {k for a in lst for k, _ in mots_titre(a["recherche"]) if k not in TYPES}
        info = []
        df, forme, avant = {}, {}, {}
        for a in lst:
            ks = {}
            mots = mots_titre(a["titre"])
            for j, (k, w) in enumerate(mots):
                if k in MOTS_VIDES or k in exclus:
                    continue
                if k not in ks and j and mots[j - 1][0] not in TYPES and mots[j - 1][0] not in exclus:
                    avant.setdefault(k, {}).setdefault(mots[j - 1][1].lower(), 0)
                    avant[k][mots[j - 1][1].lower()] += 1
                ks.setdefault(k, w)
            typ = next((TYPES[k] for k in ks if k in TYPES), None)
            info.append((a, typ, ks))
            for k, w in ks.items():
                df[k] = df.get(k, 0) + 1
                forme.setdefault(k, {}).setdefault(w, 0)
                forme[k][w] += 1
        plafond = max(MODELE_MIN, 0.6 * len(lst))  # un mot présent presque partout ne distingue rien
        for a, typ, ks in info:
            cands = [k for k in ks if k not in TYPES and MODELE_MIN <= df[k] <= plafond]
            noms = [k for k in cands if k not in MATIERES] or cands
            nom = min(noms, key=lambda k: (df[k], k)) if noms else None  # le mot le plus précis
            # Sans nom de modèle, ou avec une simple matière : type + coupe (manteau long, manteau long laine…)
            coupe = coupe_titre(a["titre"]) if not nom or nom in MATIERES else None
            g = (f"{marque}|{nom}" if nom not in MATIERES else f"{marque}|{nom}|{typ or ''}|{coupe or ''}") if nom else f"{marque}|#{typ or 'autre'}|{coupe or ''}"
            de[a["id"]] = g
            G = groupes.setdefault(g, {"cle": g, "marque": marque, "nom": nom, "coupe": coupe, "types": {}, "annonces": []})
            G["annonces"].append(a)
            if typ:
                G["types"][typ] = G["types"].get(typ, 0) + 1
            if nom:
                f = forme[nom]
                G["forme"] = max(f, key=f.get).lower()
                # Mot qui précède presque toujours le modèle (« grand » froid, « retro » pile…)
                av = avant.get(nom) or {}
                m = max(av, key=av.get) if av else None
                if m and av[m] >= 0.6 * df[nom]:
                    G["forme"] = f"{m} {G['forme']}"
    for G in groupes.values():
        typ = max(G["types"], key=G["types"].get) if G["types"] else ""
        if G["nom"]:
            mot = G["forme"]
            if G["nom"] in MATIERES:
                c = libelle_coupe(typ, G["coupe"])
                G["libelle"] = " ".join(x for x in (typ, c, mot.lower()) if x)
                G["libelle"] = G["libelle"][:1].upper() + G["libelle"][1:]
                G["mots"] = " ".join(x for x in (typ.lower(), c, mot.lower()) if x)
            else:
                mot = mot[:1].upper() + mot[1:]
                G["libelle"] = f"{typ} {mot}" if typ else mot
                G["mots"] = G["forme"]
        else:
            typ = G["cle"].split("|")[-2][1:]
            typ = "" if typ == "autre" else typ
            c = libelle_coupe(typ, G["coupe"])
            G["libelle"] = (f"{typ} {c}" if c else f"{typ} (autres)") if typ else "Autres"
            G["mots"] = f"{typ.lower()} {c}".strip() if typ else ""
        G["typ"] = typ
        lst = G["annonces"]
        vendus = [a for a in lst if a["statut"] == "vendu" and a["prix"] is not None]
        dispo = [a for a in lst if a["statut"] in ("dispo", "reserve")]
        likes = [a.get("likes") or 0 for a in dispo]
        G["vendus"], G["dispo"] = vendus, dispo
        G["v24"] = sum(1 for a in vendus if (heures(a.get("publie"), a["vendu_vu"]) or 1e9) <= 24)
        G["median"] = statistics.median(a["prix"] for a in vendus) if vendus else None
        G["achat"] = prix_achat_max(G["median"]) if vendus else None
        delais = [h for h in (heures(a.get("publie"), a["vendu_vu"]) for a in vendus) if h is not None]
        G["delai"] = statistics.median(delais) if delais else None
        G["likes"] = sum(likes) / len(likes) if likes else None
        G["part_vite"] = dominants([a for a in vendus if (heures(a.get("publie"), a["vendu_vu"]) or 1e9) <= 24] or vendus)
        G["likes_max"] = max(likes, default=0)
        rapides = sorted(vendus, key=lambda a: heures(a.get("publie"), a["vendu_vu"]) or 1e9)
        aimees = sorted(dispo, key=lambda a: a.get("likes") or 0, reverse=True)
        G["photo"] = next((a for a in rapides + aimees if a["photo"]), None)
        premier = (etat.get("modeles_vus") or {}).get(G["cle"])
        G["nouveau"] = bool(G["nom"] and premier and t0 - premier <= NOUVEAU_H * 3600
                            and (vendus or G["likes_max"] >= NOUVEAU_LIKES))
    return groupes, de


def noter_modeles(etat, t0):
    """Retient la date d'apparition de chaque modèle (badge « Nouveau »). Au tout premier passage, rien n'est nouveau."""
    groupes, _ = modeles(etat, t0)
    debut = "modeles_vus" not in etat
    vus = etat.setdefault("modeles_vus", {})
    for g, G in groupes.items():
        if G["nom"] and g not in vus:
            vus[g] = 0 if debut else t0


def dominants(vendus):
    """Couleur et taille qui reviennent le plus dans ces ventes (au moins 2 fois et 40 %)."""
    out = []
    for f in (lambda a: couleur_titre(a["titre"]), taille_courte):
        n = {}
        for a in vendus:
            v = f(a)
            if v:
                n[v] = n.get(v, 0) + 1
        if n:
            v = max(n, key=n.get)
            if n[v] >= 2 and n[v] >= 0.4 * len(vendus):
                out.append(v)
    return out


def profil_marques(etat, t0):
    """Par marque : couleurs, tailles et coupes qui partent le plus vite (et le moins vite) en moins de 24 h."""
    par = {}
    for a in etat["annonces"].values():
        if a.get("publie") and (a["statut"] == "vendu" or t0 - a["publie"] >= 24 * 3600) and a["statut"] != "supprime":
            par.setdefault(cle_marque(a), []).append(a)
    out = {}
    for marque, lst in par.items():
        if len(lst) < PROFIL_MIN * 2:
            continue
        vite = lambda a: a["statut"] == "vendu" and (heures(a["publie"], a["vendu_vu"]) or 1e9) <= 24
        base = sum(1 for a in lst if vite(a)) / len(lst)
        vals = []
        for f in (lambda a: couleur_titre(a["titre"]), taille_courte, lambda a: coupe_titre(a["titre"])):
            n = {}
            for a in lst:
                v = f(a)
                if v:
                    t = n.setdefault(v, [0, 0])
                    t[0] += 1
                    t[1] += vite(a)
            vals += [(v, k / t, t) for v, (t, k) in n.items() if t >= PROFIL_MIN]
        bons = sorted([x for x in vals if x[1] >= 1.3 * base and x[1] > 0], key=lambda x: -x[1])[:4]
        mauvais = sorted([x for x in vals if x[1] <= 0.5 * base], key=lambda x: (x[1], -x[2]))[:3]
        if bons or mauvais:
            out[marque] = {"base": base, "n": len(lst), "bons": bons, "mauvais": mauvais}
    return out


def ma_liste(groupes):
    """Les modèles à acheter : ceux qui ont le plus de ventes en moins de 24 h."""
    lst = [G for G in groupes.values() if G["mots"] and G["achat"] is not None and G["v24"] >= LISTE_MIN_RAPIDES]
    lst.sort(key=lambda G: (G["v24"], len(G["vendus"]), G["median"]), reverse=True)
    return lst[:LISTE_N]


def lien_recherche(G):
    """Recherche Vinted du modèle, très bon état, jusqu'à l'achat max, plus récentes d'abord."""
    q = urlencode({"search_text": f"{G['marque']} {G['mots']}", "price_to": round(G["achat"]),
                   "status_ids[]": 2, "currency": "EUR", "order": "newest_first"})
    return f"{BASE or 'https://www.vinted.fr'}/catalog?{q}"


def noter_liste(etat, t0):
    """Notifie quand un modèle entre dans « Ma liste » (une fois par modèle). Au tout premier passage, rien n'est notifié."""
    groupes, _ = modeles(etat, t0)
    debut = "liste_vue" not in etat
    vus = set(etat.get("liste_vue", []))
    for G in ma_liste(groupes):
        if G["cle"] in vus:
            continue
        vus.add(G["cle"])
        if not debut:
            p = G["photo"]
            notifier(f"Nouveau dans ta liste : {G['libelle']} · {G['marque']}",
                     f"{G['v24']} ventes en moins de 24 h · médian {fmt_prix(G['median'])} → achat ≤ {fmt_prix(round(G['achat']))}",
                     lien=lien_recherche(G), photo=p["photo"] if p else None, tags="star")
    etat["liste_vue"] = sorted(vus)


def chauffe(etat, t0):
    """Annonces encore en vente qui prennent le plus de likes par heure depuis leur mise en ligne."""
    out = []
    for a in etat["annonces"].values():
        if a["statut"] in ("dispo", "reserve") and a.get("publie") and (a.get("likes") or 0) >= CHAUFFE_MIN_LIKES:
            h = max(1.0, heures(a["publie"], a.get("dernier_dispo") or t0))
            out.append((a["likes"] / h, h, a))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:CHAUFFE_N]


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

    noter_modeles(etat, t0)
    if not est_nuit():
        noter_liste(etat, t0)
    etat["dernier_passage"] = t0
    ecrire_etat(etat)
    ecrire_rapport(etat)
    ecrire_quiz(etat)
    ecrire_export(etat)


# ---------------------------------------------------------------- rapport

CSS = """.haut{display:flex;align-items:center;justify-content:space-between;gap:12px}.tirer{position:fixed;top:0;left:50%;transform:translate(-50%,-48px);z-index:5;font-size:13px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:6px 12px;pointer-events:none}.boutons{display:flex;gap:8px}.boutons a{font-size:14px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:8px 14px;text-decoration:none}.haut button{font:inherit;font-size:14px;font-weight:600;color:var(--accent);background:var(--accent-soft);border:0;border-radius:99px;padding:8px 14px;cursor:pointer}
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
.filtres{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.filtres select{flex:1 1 220px;min-width:0;font:600 16px system-ui;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 12px}
[hidden]{display:none!important}
img[data-u]{cursor:zoom-in}.vue{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.92);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom))}
.vue img{max-width:100%;max-height:calc(100% - 120px);object-fit:contain;border-radius:6px}.vue p{color:#fff;margin:0;font-size:14px;text-align:center}
.vue a{background:#fff;color:#1d2120;font-weight:700;text-decoration:none;border-radius:999px;padding:10px 20px}.vue button{position:absolute;top:max(12px,env(safe-area-inset-top));right:12px;font:600 28px/1 system-ui;color:#fff;background:none;border:0;padding:8px}
.vig{display:flex;gap:4px}.vig a{display:block;width:44px;height:58px;border-radius:4px;overflow:hidden;background:var(--accent-soft)}.vig img{width:100%;height:100%;object-fit:cover}
.achat{font-weight:700;color:var(--accent)}
h3{font-size:16px;margin:0 0 8px}.go{color:var(--accent);font-weight:700;text-decoration:none}.vite{color:var(--accent);font-size:12px;font-weight:600}
.profil{background:var(--accent-soft);border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:14px}
.verif{margin-top:10px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px 14px}.verif summary{font-weight:600;cursor:pointer}.verif ol{margin:8px 0;padding-left:20px}.verif p{margin:0;font-weight:600}h3 .m{font-weight:400}#modeles{margin-bottom:16px}
.mods{display:grid;grid-auto-flow:column;grid-auto-columns:150px;gap:10px;overflow-x:auto;padding-bottom:6px;scroll-snap-type:x proximity}
.mod{font:inherit;color:inherit;text-align:left;padding:0;cursor:pointer;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;flex-direction:column;scroll-snap-align:start}
.mod .ph,.mods .card .ph{aspect-ratio:1}.mods .card{scroll-snap-align:start}.mod.on{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}.mod .body span{display:block}
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

    def zoom(a):
        """Données de la visionneuse : la photo s'ouvre en grand, avec un bouton vers l'annonce."""
        return f'data-u="{html.escape(a["url"])}" data-t="{html.escape(a["titre"])} · {fmt_prix(a["prix"])}"'

    groupes, groupe_de = modeles(etat, t0)
    vendus = sorted([a for a in ann if a["statut"] == "vendu"], key=lambda a: a["vendu_vu"], reverse=True)
    cartes = []
    for a in vendus:
        h = heures(a.get("publie"), a["vendu_vu"])
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)} '
               f'onerror="this.replaceWith(\'Photo expirée\')">') if a["photo"] else "Pas de photo"
        cartes.append(
            f'<article class="card" data-g="{html.escape(groupe_de.get(a["id"], ""))}" data-r="{html.escape(a["recherche"])}" data-m="{html.escape((a["marque"] or "").strip())}"><div class="ph">{img}<span class="b d{" hot" if h is not None and h <= 6 else ""}">≤ {fmt_h(h)}</span>'
            f'<span class="b p">{fmt_prix(a["prix"])}</span></div><div class="body"><span class="m">{html.escape(a["recherche"])}</span>'
            f'<span class="t">{html.escape(a["titre"])}</span><span class="m">{html.escape(" · ".join(x for x in (a["marque"], a["taille"], a["etat_article"]) if x))}'
            f'{" · ♥ " + str(a["likes"]) if a.get("likes") else ""}</span>'
            f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Voir l’annonce ↗</a></div></article>')

    cl = classement(etat, t0)
    def vignette(a):
        img = f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)}>' if a["photo"] else ""
        return f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener" title="{html.escape(a["titre"])} · {fmt_prix(a["prix"])}">{img}</a>'
    lignes_cl = [
        f"<tr><td>{k}</td><td>{html.escape(c['nom'])}</td><td class=\"achat\">≤ {fmt_prix(round(c['achat']))}</td><td>{fmt_prix(c['median'])}</td>"
        f"<td>{c['v24']} / {c['vendus']}</td><td>{100 * c['taux']:.0f} %</td>"
        f"<td><div class=\"vig\">{''.join(vignette(a) for a in c['exemples'])}</div></td></tr>"
        for k, c in enumerate(cl, 1)]
    section_cl = (
        f"""<section><h2>Classement de la semaine</h2><div class="tbl"><table><thead><tr><th>#</th><th>Recherche</th><th>Achat max</th><th>Prix médian vendu</th>"""
        f"""<th>Vendues &lt; 24 h</th><th>Taux 24 h</th><th>Plus rapides</th></tr></thead><tbody>"""
        f"""{''.join(lignes_cl) or f'<tr><td colspan="7">Pas encore assez de ventes (au moins {CLASSEMENT_MIN_VENTES} par recherche sur {CLASSEMENT_J} jours).</td></tr>'}</tbody></table></div>"""
        f"""<p class="sub">Sur les {CLASSEMENT_J} derniers jours. Achat max : {ACHAT_PART * 100:.0f} % du prix médian vendu, avec au moins {ACHAT_MARGE} € de marge. Les vignettes sont les ventes les plus rapides.</p></section>""")

    # Modèles : une carte par modèle, affichées pour la marque choisie (les 8 meilleurs toutes marques sinon)
    mods = [G for G in groupes.values() if (G["vendus"] or G["nouveau"]) and len(G["annonces"]) >= 2]
    mods.sort(key=lambda G: (bool(G["nom"]), len(G["vendus"]), G["v24"], G["likes"] or 0), reverse=True)
    top = {G["cle"] for G in sorted([G for G in mods if G["nom"] or G["coupe"]], key=lambda G: (G["v24"], len(G["vendus"])), reverse=True)[:8]} | {G["cle"] for G in mods if G["nouveau"]}
    cartes_mod = []
    NOUVEAU_BADGE = '<span class="b d hot">Nouveau</span>'
    for G in mods:
        p = G["photo"]
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(p["photo"])}">' if p else "Pas de photo")
        achat = f'<span class="b p">≤ {fmt_prix(round(G["achat"]))}</span>' if G["achat"] is not None else ""
        vite = f'<span class="vite">Part vite : {html.escape(" · ".join(G["part_vite"]))}</span>' if G["part_vite"] else ""
        dispo = f'{len(G["dispo"])} en vente' + (f' · ♥ {G["likes"]:.0f} en moy.' if G["dispo"] else "")
        cartes_mod.append(
            f'<button type="button" class="mod" data-g="{html.escape(G["cle"])}" data-m="{html.escape(G["marque"].lower())}"{" data-top" if G["cle"] in top else ""}>'
            f'<span class="ph">{img}{NOUVEAU_BADGE if G["nouveau"] else ""}'
            f'{achat}</span><span class="body">'
            f'<span class="m">{html.escape(G["marque"])}</span><span class="t">{html.escape(G["libelle"])}</span>'
            f'<span>{len(G["vendus"])} vendue{"s" if len(G["vendus"]) > 1 else ""}{" · médian " + fmt_prix(G["median"]) if G["vendus"] else ""}</span>'
            f'<span class="m">{G["v24"]} en &lt; 24 h{" · délai médian " + fmt_h(G["delai"]) if G["delai"] is not None else ""}</span><span class="m">{dispo}</span>{vite}</span></button>')
    def pct(x):
        return f"{100 * x:.0f} %"
    profils = []
    for marque, P in sorted(profil_marques(etat, t0).items()):
        bons = " · ".join(f"<b>{html.escape(v)}</b> {pct(r)}" for v, r, _ in P["bons"]) or "rien qui se détache"
        mauvais = " · ".join(f"{html.escape(v)} {pct(r)}" for v, r, _ in P["mauvais"])
        profils.append(
            f'<div class="profil" data-m="{html.escape(marque.lower())}" hidden><b>{html.escape(marque)}</b> : {pct(P["base"])} des annonces partent en moins de 24 h '
            f'({P["n"]} suivies).<br>Part vite : {bons}{"<br>Part mal : " + mauvais if mauvais else ""}</div>')
    section_mod = (
        f'<div id="modeles"{"" if cartes_mod else " hidden"}><h3>Modèles <span class="m" id="mod-aide">les plus rapides, toutes marques : choisis une marque pour voir les siens</span></h3>{"".join(profils)}'
        f'<div class="mods">{"".join(cartes_mod)}</div><p class="sub">Modèles repérés aux mots qui reviennent dans les titres (au moins {MODELE_MIN} annonces). '
        f'Sans nom de modèle, les annonces sont groupées par type et coupe (manteau long, doudoune sans manches…). Couleur, taille et coupe : part vendue en moins de 24 h, sur au moins {PROFIL_MIN} annonces. '
        f'Le prix en bas de la photo est l’achat max. Touche un modèle pour ne garder que ses ventes, retouche-le pour tout revoir.</p></div>')

    liste = ma_liste(groupes)
    lignes_li = []
    for k, G in enumerate(liste, 1):
        p = G["photo"]
        img = f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(p["photo"])}" {zoom(p)}>' if p else ""
        lignes_li.append(
            f'<tr><td>{k}</td><td><div class="vig"><a>{img}</a></div></td><td><b>{html.escape(G["libelle"])}</b> <span class="m">{html.escape(G["marque"])}{" · " + html.escape(" · ".join(G["part_vite"])) if G["part_vite"] else ""}</span><br>'
            f'<a class="go" href="{html.escape(lien_recherche(G))}" target="_blank" rel="noopener">Chercher ≤ {fmt_prix(round(G["achat"]))} ↗</a></td>'
            f'<td class="achat">≤ {fmt_prix(round(G["achat"]))}</td><td>{fmt_prix(G["median"])}</td><td>{G["v24"]} / {len(G["vendus"])}</td></tr>')
    section_li = (
        f"""<section><h2>Ma liste</h2><div class="tbl"><table><thead><tr><th>#</th><th></th><th>Modèle</th><th>Achat max</th><th>Médian vendu</th>"""
        f"""<th>Vendues &lt; 24 h</th></tr></thead><tbody>"""
        f"""{''.join(lignes_li) or f'<tr><td colspan="6">Aucun modèle n’a encore {LISTE_MIN_RAPIDES} ventes en moins de 24 h.</td></tr>'}</tbody></table></div>"""
        f"""<p class="sub">Les {LISTE_N} modèles au maximum qui ont au moins {LISTE_MIN_RAPIDES} ventes en moins de 24 h. Tout le reste, tu l’ignores. """
        f"""Un modèle qui entre dans la liste arrive aussi sur ntfy. « Chercher » ouvre Vinted en très bon état jusqu’à l’achat max : c’est aussi le filtre à recopier dans Kops.</p>"""
        f"""<details class="verif"><summary>Vérifier une annonce en 30 secondes</summary><ol>"""
        f"""<li>Étiquette de marque visible en photo.</li><li>Prix ≤ achat max du modèle.</li><li>Très bon état, sans défaut, tache ni trou.</li>"""
        f"""<li>Vraies photos (pas de photos catalogue).</li><li>Vendeur avec des avis.</li></ol>"""
        f"""<p>Tout est bon : tu achètes. Tu hésites : tu passes.</p></details></section>""")

    ch = chauffe(etat, t0)
    cartes_ch = []
    for _, h, a in ch:
        img = (f'<img loading="lazy" referrerpolicy="no-referrer" alt="" src="{html.escape(a["photo"])}" {zoom(a)} '
               f'onerror="this.replaceWith(\'Photo expirée\')">') if a["photo"] else "Pas de photo"
        G = groupes.get(groupe_de.get(a["id"], ""))
        achat = f'achat ≤ {fmt_prix(round(G["achat"]))}' if G and G["achat"] is not None and G["nom"] else ""
        cartes_ch.append(
            f'<article class="card"><div class="ph">{img}<span class="b d hot">♥ {a["likes"]} en {fmt_h(h)}</span>'
            f'<span class="b p">{fmt_prix(a["prix"])}</span></div><div class="body"><span class="m">{html.escape(G["libelle"] + " · " if G and G["nom"] else "")}{html.escape(a["marque"] or a["recherche"])}</span>'
            f'<span class="t">{html.escape(a["titre"])}</span><span class="m">{html.escape(" · ".join(x for x in (a["taille"], achat) if x))}</span>'
            f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Voir l’annonce ↗</a></div></article>')
    section_ch = (
        f'<section><h2>Ça chauffe</h2><div class="mods">{"".join(cartes_ch)}</div>'
        f'<p class="sub">Annonces encore en vente qui prennent le plus de likes par heure depuis leur mise en ligne (au moins {CHAUFFE_MIN_LIKES} likes).</p></section>'
        if cartes_ch else "")

    maj = datetime.fromtimestamp(t0, ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>Ventes suivies</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icone-180.png"><meta name="theme-color" content="#2f5d50">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Ventes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><style>{CSS}</style></head><body><div class="tirer" id="tirer">↓ Tirer pour actualiser</div><div class="wrap">
<header><div class="haut"><h1>Ventes suivies</h1><div class="boutons"><a href="quiz.html">Quiz</a><button type="button" id="actualiser">↻ Actualiser</button></div></div><p class="sub">Mis à jour le {maj}. Une annonce est suivie pendant {FENETRE_SUIVI_H} h après sa publication.
Le délai « ≤ » est le temps entre la mise en ligne et le moment où la vente a été repérée : la vente réelle a eu lieu avant.</p></header>
{section_li}
{section_ch}
{section_cl}
<section><h2>Par recherche</h2><div class="tbl"><table><thead><tr><th>Recherche</th><th>Suivies</th><th>Vendues</th><th>En &lt; 24 h</th><th>Taux 24 h</th><th>Délai médian</th><th>Prix médian</th></tr></thead>
<tbody>{''.join(lignes) or '<tr><td colspan="7">Aucune donnée pour l’instant.</td></tr>'}</tbody></table></div>
<p class="sub">Taux 24 h : part des annonces vendues en moins de 24 h, parmi celles publiées depuis au moins 24 h ou déjà vendues.</p></section>
<section><h2>Vendues ({len(vendus)})</h2><div class="filtres"><select id="f-m" aria-label="Marque" hidden></select><select id="f-r" aria-label="Recherche" hidden></select></div>{section_mod}<div class="grid" id="grid">{''.join(cartes)}</div></section></div>
<div class="vue" id="vue" hidden><button type="button" aria-label="Fermer">×</button><img alt="" referrerpolicy="no-referrer"><p></p><a target="_blank" rel="noopener">Voir sur Vinted ↗</a></div>
<script>(function(){{var g=document.getElementById('grid'),cartes=[].slice.call(g.querySelectorAll('.card')),choix={{r:'',m:'',g:''}},
mods=[].slice.call(document.querySelectorAll('.mod')),aide=document.getElementById('mod-aide');
function cle(e,a){{return a==='m'?(e.dataset.m||'').toLowerCase():e.dataset.r}}
function appliquer(){{cartes.forEach(function(e){{e.hidden=(!!choix.r&&cle(e,'r')!==choix.r)||(!!choix.m&&cle(e,'m')!==choix.m)||(!!choix.g&&e.dataset.g!==choix.g)}});
var n=0;mods.forEach(function(e){{e.hidden=choix.m?e.dataset.m!==choix.m:!e.hasAttribute('data-top');if(!e.hidden)n++;e.classList.toggle('on',e.dataset.g===choix.g)}});
[].forEach.call(document.querySelectorAll('.profil'),function(e){{e.hidden=e.dataset.m!==choix.m}});
if(aide)aide.textContent=choix.m?'de cette marque, du plus vendu au moins vendu':'les plus rapides, toutes marques : choisis une marque pour voir les siens';
document.getElementById('modeles').hidden=!n}}
mods.forEach(function(e){{e.onclick=function(){{choix.g=choix.g===e.dataset.g?'':e.dataset.g;appliquer();if(choix.g)g.scrollIntoView({{behavior:'smooth',block:'start'}})}}}});
function menu(id,a,tout){{var c=document.getElementById(id),n={{}},nom={{}};
cartes.forEach(function(e){{var k=cle(e,a);if(!k)return;n[k]=(n[k]||0)+1;nom[k]=nom[k]||(a==='m'?e.dataset.m:k)}});
var k=Object.keys(n).sort(function(x,y){{return nom[x].localeCompare(nom[y],'fr')}});if(k.length<2)return;
[''].concat(k).forEach(function(r){{var o=document.createElement('option');o.value=r;o.textContent=(r?nom[r]:tout)+' ('+(r?n[r]:cartes.length)+')';c.appendChild(o)}});
c.hidden=false;c.onchange=function(){{choix[a]=c.value;choix.g='';appliquer()}}}}
menu('f-m','m','Toutes les marques');menu('f-r','r','Toutes les recherches');appliquer()}})();
(function(){{var v=document.getElementById('vue'),im=v.querySelector('img'),t=v.querySelector('p'),l=v.querySelector('a');
function fermer(){{v.hidden=true;im.removeAttribute('src')}}
document.addEventListener('click',function(e){{var x=e.target.closest('img[data-u]');if(!x)return;e.preventDefault();
im.src=x.currentSrc||x.src;t.textContent=x.dataset.t;l.href=x.dataset.u;v.hidden=false}});
v.addEventListener('click',function(e){{if(e.target===v||e.target.tagName==='BUTTON')fermer()}});
document.addEventListener('keydown',function(e){{if(e.key==='Escape')fermer()}})}})();
(function(){{var t0=Date.now();function recharger(){{location.replace(location.pathname+'?t='+Date.now())}}
document.getElementById('actualiser').onclick=recharger;
var ind=document.getElementById('tirer'),y0=null,dy=0,SEUIL=90;
function poser(d){{ind.style.transform='translate(-50%,'+(Math.min(d,SEUIL+20)-48)+'px)';ind.textContent=d>=SEUIL?'↻ Relâcher pour actualiser':'↓ Tirer pour actualiser'}}
document.addEventListener('touchstart',function(e){{y0=(window.scrollY<=0&&document.getElementById('vue').hidden)?e.touches[0].clientY:null;dy=0}},{{passive:true}});
document.addEventListener('touchmove',function(e){{if(y0===null)return;dy=e.touches[0].clientY-y0;if(dy>0)poser(dy)}},{{passive:true}});
document.addEventListener('touchend',function(){{if(y0===null)return;y0=null;if(dy>=SEUIL){{ind.textContent='↻ Actualisation…';recharger()}}else poser(0)}});
document.addEventListener('visibilitychange',function(){{if(!document.hidden&&Date.now()-t0>120000)recharger()}})}})();</script></body></html>"""
    os.makedirs(os.path.dirname(FICHIER_RAPPORT), exist_ok=True)
    with open(FICHIER_RAPPORT, "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- quiz

FICHIER_QUIZ = os.path.join(ICI, "docs", "quiz.html")
QUIZ_N = 24                 # modèles au maximum dans le quiz (ceux de « Ma liste » d'abord)
QUIZ_MIN_VENTES = 3         # ventes minimum pour qu'un modèle entre dans le quiz
# Pièges : toujours passer (règles d'achat)
PIEGES = [
    {"marque": "Parajumpers", "nom": "Doudoune", "piege": "Abandonné : trop de contrefaçons."},
    {"marque": "K-Way", "nom": "Coupe-vent", "piege": "Écarté : risque de contrefaçons."},
    {"marque": "Sandro", "nom": "Blazer seul", "piege": "Un blazer seul en très bon état se revend 33 € : pas de marge possible."},
    {"marque": "Paragoose", "nom": "Doudoune (hors Aria)", "piege": "Adulte très bon état : 30 € de médiane, achat ≤ 12 €. Ne vaut pas le temps."},
    {"marque": "Woolrich", "nom": "Parka sans « Arctic »", "piege": "Sans le nom Arctic, médiane 50 € : achat ≤ 20 € seulement."},
]

QUIZ_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>Quiz des modèles</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icone-180.png"><meta name="theme-color" content="#2f5d50">
<style>
:root{--bg:#f6f6f3;--surface:#fff;--ink:#1d2120;--muted:#646b68;--line:#dfe1dc;--accent:#2f5d50;--accent-soft:#e3ece8;--ok:#1d7a55;--ok-bg:#dcefe5;--ko:#b8402a;--ko-bg:#f6e1da;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141716;--surface:#1d2120;--ink:#ecefec;--muted:#9aa29e;--line:#2e3431;--accent:#8cc2ad;--accent-soft:#22312b;--ok:#5cc79a;--ok-bg:#16352a;--ko:#ee8a74;--ko-bg:#3d2019;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,sans-serif;padding:max(20px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 40px max(16px,env(safe-area-inset-left))}
.wrap{max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px}h1{font-size:24px;margin:0}
header a{color:var(--accent);font-weight:600;text-decoration:none;font-size:14px}.score{color:var(--muted);font-size:14px;font-variant-numeric:tabular-nums}
.modes{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px}.modes button{flex:0 0 auto;font:600 14px system-ui;color:var(--accent);background:var(--accent-soft);border:0;border-radius:99px;padding:8px 13px;cursor:pointer}
.modes button[aria-pressed=true]{background:var(--accent);color:var(--bg)}
.carte{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.ph{aspect-ratio:4/5;max-height:55vh;width:100%;background:var(--accent-soft);display:grid;place-items:center;color:var(--muted);position:relative}.ph img{width:100%;height:100%;object-fit:cover}.carte>.ph{margin:0 auto}
.ph .b{position:absolute;bottom:10px;left:10px;background:var(--ink);color:var(--bg);font-weight:700;border-radius:6px;padding:3px 9px;font-size:18px}
.pad{padding:14px;display:flex;flex-direction:column;gap:10px}.q{font-size:19px;font-weight:700;margin:0}.m{color:var(--muted);font-size:14px}
.choix{display:grid;grid-template-columns:1fr 1fr;gap:8px}.choix button{font:600 17px system-ui;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:13px 8px;cursor:pointer;font-variant-numeric:tabular-nums}
.choix button.ok{background:var(--ok-bg);border-color:var(--ok);color:var(--ok)}.choix button.ko{background:var(--ko-bg);border-color:var(--ko);color:var(--ko)}
.fb{border-radius:10px;padding:10px 12px;font-size:15px}.fb.ok{background:var(--ok-bg)}.fb.ko{background:var(--ko-bg)}.fb b{display:block;margin-bottom:2px}
.fiche{font-size:14px;display:grid;grid-template-columns:auto 1fr;gap:3px 12px}.fiche dt{color:var(--muted)}.fiche dd{margin:0;font-weight:600}
.suite{font:700 16px system-ui;color:var(--bg);background:var(--accent);border:0;border-radius:10px;padding:13px;cursor:pointer}
.go{color:var(--accent);font-weight:700;text-decoration:none}
.liste{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.liste .ph{aspect-ratio:1}.liste .pad{padding:10px;gap:4px;font-size:14px}
.vig{display:flex;gap:4px}.vig img{width:48px;height:62px;object-fit:cover;border-radius:4px;background:var(--accent-soft)}
.piege{background:var(--ko-bg);border-radius:10px;padding:8px 10px;font-size:14px}.regle{color:var(--muted);font-size:13px;margin:0}
</style></head><body><div class="wrap">
<header><h1>Quiz des modèles</h1><a href="./">← Ventes</a></header>
<div class="modes" id="modes"><button data-mode="mix" aria-pressed="true">Mélange</button><button data-mode="revente" aria-pressed="false">Revente ?</button><button data-mode="achat" aria-pressed="false">Achat max ?</button><button data-mode="buy" aria-pressed="false">J'achète ?</button><button data-mode="qui" aria-pressed="false">Quel modèle ?</button><button data-mode="fiches" aria-pressed="false">Fiches</button></div>
<div class="score" id="score"></div>
<main id="app"></main>
<p class="regle">Modèles et prix tirés automatiquement des ventes suivies (mis à jour le __MAJ__). Très bon état seulement, achat ≤ 40 % du médian vendu avec au moins 30 € de marge. Les modèles que tu rates reviennent plus souvent.</p>
</div>
<script>
var M=__DATA__, P=__PIEGES__;
var app=document.getElementById('app'),mode='mix',score={ok:0,n:0},dernier=null,mem={};
try{mem=JSON.parse(localStorage.getItem('quiz-auto')||'{}')}catch(e){}
function sauver(){try{localStorage.setItem('quiz-auto',JSON.stringify(mem))}catch(e){}}
function st(id){return mem[id]||(mem[id]={ok:0,ko:0})}
function rnd(a){return a[Math.floor(Math.random()*a.length)]}
function esc(t){var d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML}
function eur(v){return Math.round(v)+' €'}
function tirer(){var tot=0,w=M.map(function(m){var s=st(m.id),x=Math.max(.3,1+2*s.ko-.5*s.ok);if(m.id===dernier)x=0;tot+=x;return x});
 var r=Math.random()*tot;for(var i=0;i<M.length;i++){r-=w[i];if(r<=0)return M[i]}return M[0]}
function leurres(v){var out=[v],f=[.55,.7,.8,1.25,1.45,1.7];f.sort(function(){return Math.random()-.5});
 for(var i=0;i<f.length&&out.length<4;i++){var x=Math.max(5,Math.round(v*f[i]/5)*5);if(out.every(function(o){return Math.abs(o-x)>=Math.max(5,v*.12)}))out.push(x)}
 return out.sort(function(a,b){return a-b})}
function photo(m,prix){var p=m.photos.length?rnd(m.photos):null;
 return '<div class="ph">'+(p?'<img alt="" referrerpolicy="no-referrer" src="'+esc(p[0])+'" onerror="this.remove()">':'Pas de photo')+(prix?'<span class="b">'+esc(prix)+'</span>':'')+'</div>'}
function fiche(m){return '<dl class="fiche"><dt>Revente</dt><dd>'+eur(m.rev)+' (médian de '+m.n+' ventes)</dd><dt>Achat max</dt><dd>≤ '+eur(m.max)+'</dd>'+
 '<dt>Vendues &lt; 24 h</dt><dd>'+m.v24+' / '+m.n+'</dd>'+(m.vite?'<dt>Part vite</dt><dd>'+esc(m.vite)+'</dd>':'')+'</dl>'+
 '<a class="go" href="'+esc(m.lien)+'" target="_blank" rel="noopener">Chercher ≤ '+eur(m.max)+' sur Vinted ↗</a>'}
function points(){document.getElementById('score').textContent=score.n?score.ok+' / '+score.n+' bonnes':''}
function poser(m,html,choix,bon,expl,cle){
 app.innerHTML='<div class="carte">'+html+'<div class="pad"><div class="choix">'+choix.map(function(c,i){return '<button data-i="'+i+'">'+esc(c)+'</button>'}).join('')+'</div><div id="apres"></div></div></div>';
 [].forEach.call(app.querySelectorAll('.choix button'),function(b){b.onclick=function(){
  var i=+b.dataset.i,juste=i===bon;[].forEach.call(app.querySelectorAll('.choix button'),function(x,j){x.disabled=true;if(j===bon)x.classList.add('ok');else if(j===i)x.classList.add('ko')});
  score.n++;if(juste)score.ok++;points();if(cle){var s=st(cle);juste?s.ok++:s.ko++;sauver()}
  document.getElementById('apres').innerHTML='<div class="fb '+(juste?'ok':'ko')+'"><b>'+(juste?'Bien vu':'Raté')+'</b>'+expl+'</div>'+(m?fiche(m):'')+'<button class="suite" id="suite">Suivante →</button>';
  document.getElementById('suite').onclick=question;document.getElementById('suite').focus()}})}
function titre(m,q){return '<div class="pad" style="padding-bottom:0"><span class="m">'+esc(m.marque)+'</span><p class="q">'+esc(m.nom)+'</p><span class="m">'+q+'</span></div>'}
function qPrix(m,cle){var v=cle==='rev'?m.rev:m.max,c=leurres(v);
 poser(m,photo(m)+titre(m,cle==='rev'?'Il se revend combien ?':'Tu l’achètes au maximum à combien ?'),c.map(eur),c.indexOf(v),
  cle==='rev'?'Médian vendu : '+eur(m.rev)+'.':'Achat max : '+eur(m.max)+' (40 % de '+eur(m.rev)+', au moins 30 € de marge).',m.id)}
function qQui(m){var autres=M.filter(function(x){return x.id!==m.id&&x.nom!==m.nom});
 var meme=autres.filter(function(x){return x.marque===m.marque}),noms=[m.nom];
 meme.concat(autres.sort(function(){return Math.random()-.5})).forEach(function(x){if(noms.length<4&&noms.indexOf(x.nom)<0)noms.push(x.nom)});
 noms.sort(function(){return Math.random()-.5});
 poser(m,photo(m)+'<div class="pad" style="padding-bottom:0"><span class="m">'+esc(m.marque)+'</span><p class="q">Quel modèle ?</p></div>',noms,noms.indexOf(m.nom),'C’est '+esc(m.nom)+'.',m.id)}
function qBuy(m){
 if(P.length&&Math.random()<.2){var p=rnd(P),prix=rnd([15,20,25,30,35]);
  app.innerHTML='';poser(null,'<div class="pad" style="padding-bottom:0"><span class="m">'+esc(p.marque)+'</span><p class="q">'+esc(p.nom)+' à '+prix+' €</p><span class="m">Très bon état. Tu achètes ?</span></div>',
   ['J’achète','Je passe'],1,'<div class="piege">'+esc(p.piege)+'</div>','piege-'+p.marque);return}
 var oui=Math.random()<.5,prix=Math.max(5,Math.round(oui?m.max*(.55+.45*Math.random()):m.max*(1.2+.7*Math.random())));
 poser(m,photo(m,prix+' €')+titre(m,'Très bon état, '+prix+' €. Tu achètes ?'),['J’achète','Je passe'],oui?0:1,
  oui?prix+' € ≤ achat max '+eur(m.max)+' : tu achètes.':prix+' € > achat max '+eur(m.max)+' : tu passes.',m.id)}
function fiches(){app.innerHTML='<div class="liste">'+M.map(function(m){return '<div class="carte">'+photo(m)+'<div class="pad"><span class="m">'+esc(m.marque)+'</span><b>'+esc(m.nom)+'</b>'+
 '<span>Revente '+eur(m.rev)+' · <b>≤ '+eur(m.max)+'</b></span><span class="m">'+m.v24+' / '+m.n+' en &lt; 24 h'+(m.vite?' · '+esc(m.vite):'')+'</span><a class="go" href="'+esc(m.lien)+'" target="_blank" rel="noopener">Chercher ↗</a></div></div>'}).join('')+'</div>'+
 (P.length?'<h2 style="font-size:18px">Pièges : toujours passer</h2>'+P.map(function(p){return '<div class="piege"><b>'+esc(p.marque)+' · '+esc(p.nom)+'</b><br>'+esc(p.piege)+'</div>'}).join('<div style="height:6px"></div>'):'')}
function question(){if(!M.length){app.innerHTML='<p class="m">Pas encore assez de ventes pour le quiz.</p>';return}
 if(mode==='fiches')return fiches();var m=tirer();dernier=m.id;
 var t=mode==='mix'?rnd(['revente','achat','buy','buy','qui']):mode;
 if(t==='revente')qPrix(m,'rev');else if(t==='achat')qPrix(m,'max');else if(t==='qui'&&M.length>=2)qQui(m);else qBuy(m);window.scrollTo(0,0)}
[].forEach.call(document.querySelectorAll('#modes button'),function(b){b.onclick=function(){mode=b.dataset.mode;
 [].forEach.call(document.querySelectorAll('#modes button'),function(x){x.setAttribute('aria-pressed',x===b)});question()}});
question();
</script></body></html>"""


def ecrire_quiz(etat):
    """Quiz des modèles, régénéré à chaque passage depuis « Ma liste » et les cartes Modèles, avec les photos des ventes."""
    t0 = etat.get("dernier_passage") or maintenant()
    groupes, _ = modeles(etat, t0)
    liste = ma_liste(groupes)
    autres = sorted([G for G in groupes.values() if G["mots"] and G["achat"] is not None and len(G["vendus"]) >= QUIZ_MIN_VENTES and G not in liste],
                    key=lambda G: (G["v24"], len(G["vendus"])), reverse=True)
    data = []
    for G in [G for G in liste + autres if G["nom"] or G["coupe"]][:QUIZ_N]:  # sans les groupes « (autres) », trop vagues
        rapides = sorted(G["vendus"], key=lambda a: heures(a.get("publie"), a["vendu_vu"]) or 1e9)
        data.append({
            "id": G["cle"], "marque": G["marque"], "nom": G["libelle"], "rev": round(G["median"]), "max": round(G["achat"]),
            "v24": G["v24"], "n": len(G["vendus"]), "vite": " · ".join(G["part_vite"]), "lien": lien_recherche(G),
            "photos": [[a["photo"], fmt_prix(a["prix"])] for a in rapides if a["photo"]][:4],
        })
    maj = datetime.fromtimestamp(t0, ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    js = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")
    page = QUIZ_HTML.replace("__DATA__", js(data)).replace("__PIEGES__", js(PIEGES)).replace("__MAJ__", maj)
    with open(FICHIER_QUIZ, "w", encoding="utf-8") as f:
        f.write(page)


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
    sommaire = ["Ventes par recherche (délai | prix | taille | titre | vendue le | likes | annonce | photo)", ""]
    for nom, lst in sorted(par.items()):
        pages = [lst[k:k + EXPORT_LIGNES] for k in range(0, len(lst), EXPORT_LIGNES)]
        fichiers = []
        for n, morceau in enumerate(pages, 1):
            fichier = f"{slug(nom)}-{n}.txt"
            lignes = [f"{nom} · page {n}/{len(pages)} · {len(lst)} ventes"]
            for a in morceau:
                quand = datetime.fromtimestamp(a["vendu_vu"], paris).strftime("%d/%m %H:%M")
                lignes.append(" | ".join([fmt_h(heures(a.get("publie"), a["vendu_vu"])), fmt_prix(a["prix"]), a["taille"] or "?",
                                          " ".join(a["titre"].split()), quand, str(a.get("likes") or 0), a["url"].split("?")[0], a["photo"] or "-"]))
            with open(os.path.join(DOSSIER_EXPORT, fichier), "w", encoding="utf-8") as f:
                f.write("\n".join(lignes) + "\n")
            fichiers.append(fichier)
        sommaire.append(f"{nom} ({len(lst)}) : {' '.join(fichiers)}")
    with open(os.path.join(DOSSIER_EXPORT, "index.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sommaire) + "\n")


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
