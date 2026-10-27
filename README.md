# Suivi des ventes

Environ toutes les 15 minutes, GitHub lance `suivi.py` (2 passages par lancement, puis le lancement suivant démarre tout seul ; une tâche horaire relance la chaîne si elle s'arrête) :

- il récupère les nouvelles annonces de chaque recherche enregistrée dans le secret `RECHERCHES` ;
- il revérifie les annonces déjà suivies ;
- dès qu'une annonce part en moins de 24 h, ton iPhone reçoit une notification (prix, délai, photo, lien) ;
- la page `docs/index.html` récapitule les ventes par recherche : taux de vente en 24 h, délai médian, prix médian, photos.

Aucune connexion à un compte : le script lit seulement des pages publiques.

## Installation (une seule fois, ~10 min)

1. **Notifications** : installe l'app gratuite **ntfy** sur l'iPhone, touche **+** et abonne-toi à un nom de sujet difficile à deviner, par exemple `ventes-emeric-7k2p9`.
2. **Dépôt** : sur github.com, **New repository**, nom neutre (ex. `suivi-prix`), **Public** (minutes de calcul illimitées et page de rapport gratuite).
3. **Fichiers** : dans le dépôt, **Add file › Upload files**, puis glisse tout le contenu du dossier dézippé. Le dossier `.github` est masqué sur Mac : dans le Finder, appuie sur **⌘ + ⇧ + .** pour l'afficher avant de glisser. Clique **Commit changes**.
4. **Secrets** : **Settings › Secrets and variables › Actions › New repository secret**, deux fois :
   - nom `NTFY_TOPIC`, valeur : ton nom de sujet ntfy ;
   - nom `RECHERCHES`, valeur : le contenu de ton fichier `recherches.txt` (gardé sur ton Mac, jamais dans le dépôt).
5. **Page de rapport** : **Settings › Pages**, Source **GitHub Actions**. Après le premier passage normal, la page sera à l'adresse `https://<ton-pseudo>.github.io/<nom-du-dépôt>/`.
6. **Test** : onglet **Actions**, active les workflows si GitHub le demande, choisis **Suivi des ventes › Run workflow**, mode **test**. Tu dois recevoir « Test réussi » sur l'iPhone. Le journal du passage affiche les annonces trouvées.
7. **App sur l'iPhone** : ouvre la page de rapport dans **Safari**, touche le bouton **Partager**, puis **Sur l'écran d'accueil**. L'icône « Ventes » apparaît comme une app et s'ouvre en plein écran.

Ensuite tout tourne seul. La page de rapport est republiée toutes les 30 minutes environ.

## Ajouter ou modifier une recherche

Les recherches ne sont pas dans le dépôt : elles sont dans le secret `RECHERCHES`, que personne d'autre ne peut lire.

1. Ouvre ton fichier `recherches.txt` sur ton Mac et ajoute une ligne `Nom affiché | adresse de la recherche`.
2. Copie tout le fichier.
3. **Settings › Secrets and variables › Actions**, crayon ✏️ à côté de `RECHERCHES`, colle, **Update secret**.

GitHub ne réaffiche jamais un secret : c'est ton fichier `recherches.txt` qui fait foi.

## Ajouter une recherche sans toucher au secret

Ouvre `recherches_ajoutees.txt` dans le dépôt, crayon ✏️, ajoute une ligne `Nom affiché | adresse de la recherche`, puis **Commit changes**. Elle est suivie dès le passage suivant, comme celles du secret. Son sujet ntfy de marque est le premier mot du nom (ex. `Patagonia` → `ton-sujet-patagonia`). Si le mot-clé est un mot courant, ajoute `&marque=Nom exact` à la fin de l'adresse : seules les annonces de cette marque sont gardées (ex. Soeur).

Une ligne qui reprend exactement le nom d'une recherche du secret la remplace, en gardant son historique (ex. `Barbour matelassée`, passée au filtre marque Barbour).

## Changer un prix minimum

Pas besoin de toucher au secret : ouvre `prix_min.txt` dans le dépôt, crayon ✏️, une ligne `Nom de la recherche | prix`, puis **Commit changes**. Ce prix remplace celui de l'adresse dès le passage suivant ; supprimer la ligne remet celui de l'adresse.

## Retirer une recherche

Ouvre `recherches_retirees.txt` dans le dépôt, crayon ✏️, ajoute le nom de la recherche (exactement comme dans le secret), puis **Commit changes**. Elle n'est plus suivie dès le passage suivant ; supprimer la ligne la remet en route.

## Réglages

En haut de `suivi.py` :

| Réglage | Par défaut | Rôle |
|---|---|---|
| `NOTIF_MAX_H` | 24 | notifier seulement les ventes parties en moins de X heures |
| `FENETRE_SUIVI_H` | 72 | durée de suivi d'une annonce après sa publication |
| `PAGES_PAR_RECHERCHE` | 2 | pages de 96 annonces lues par recherche |
| `MAX_VERIFS_PAR_PASSAGE` | 250 | annonces revérifiées au maximum par passage |
| `ETATS` | ["2"] | états gardés pour toutes les recherches, quels que soient ceux des adresses (2 = très bon état) |
| `AFFAIRES` | True | chercher, pour chaque article vendu, le même en ligne à 40 % ou moins de son prix (section « Même article, 2,5× moins cher » en haut de la page) |
| `MAX_AFFAIRES_PAR_PASSAGE` | 10 | recherches « même article » au maximum par passage (une page de résultats chacune) |
| `AFFAIRES_RELANCE_H` / `AFFAIRES_FENETRE_H` | 12 / 72 | recherche relancée toutes les 12 h pendant 72 h après la vente |
| `AFFAIRES_MOTS` | 2 | mots du titre vendu gardés pour la recherche (hors marque, couleur, taille) |
| `NOTIF_AFFAIRES` | True | notification sur le sujet principal pour chaque nouvelle bonne affaire (pas la nuit) |
| `CLASSEMENT_J` | 7 | période du classement de la semaine |
| `CLASSEMENT_MIN_VENTES` | 3 | ventes minimum pour qu'une recherche soit classée |
| `ACHAT_PART` / `ACHAT_MARGE` | 0.40 / 30 | prix d'achat max : 40 % du prix médian vendu, avec au moins 30 € de marge |
| `RESUME_JOUR`, `RESUME_HEURE` | 0, 8 | résumé de la semaine sur ntfy le lundi à partir de 8 h |

### Notifications

Chaque vente arrive une seule fois, sur ton sujet principal. Pour filtrer par marque, utilise les boutons de la page. Pour recevoir aussi une copie silencieuse sur un sujet par marque (`ton-sujet-uniqlo`, `ton-sujet-bash` pour Ba&sh…), passe `COPIE_MARQUE` à `True` en tête de `suivi.py`.

Pour ne plus recevoir de notifications sans arrêter le suivi, supprime le secret `NTFY_TOPIC`. Pour tout arrêter : **Actions › Suivi des ventes › ⋯ › Disable workflow**.

### Classement de la semaine

En haut de la page, les recherches sont classées sur les 7 derniers jours : ventes en moins de 24 h, part vendue en moins de 24 h, prix médian vendu et prix d'achat max, avec les photos des ventes les plus rapides. Chaque lundi matin, les 5 premières arrivent aussi sur le sujet ntfy principal, avec un lien vers la page.

## Limites

- Le délai affiché est un maximum : la vente a eu lieu entre le dernier passage où l'annonce était disponible et celui où elle a été vue vendue.
- Une annonce supprimée par le vendeur n'est pas comptée comme vendue.
- Les photos des annonces expirent au bout de quelques jours ; les liens restent valables.
- Le dépôt est public mais ne contient que le code : ni recherches, ni sujet ntfy, ni annonces. La mémoire du suivi reste dans le cache interne de GitHub, et la page de rapport est publiée sans passer par les fichiers du dépôt. Elle reste lisible par quiconque connaît son adresse.
- Le cache GitHub s'efface après 7 jours sans passage : si le suivi est arrêté plus d'une semaine, il repart de zéro.
