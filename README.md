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

## Changer un prix minimum

Pas besoin de toucher au secret : ouvre `prix_min.txt` dans le dépôt, crayon ✏️, une ligne `Nom de la recherche | prix`, puis **Commit changes**. Ce prix remplace celui de l'adresse dès le passage suivant ; supprimer la ligne remet celui de l'adresse.

## Réglages

En haut de `suivi.py` :

| Réglage | Par défaut | Rôle |
|---|---|---|
| `NOTIF_MAX_H` | 24 | notifier seulement les ventes parties en moins de X heures |
| `FENETRE_SUIVI_H` | 72 | durée de suivi d'une annonce après sa publication |
| `PAGES_PAR_RECHERCHE` | 2 | pages de 96 annonces lues par recherche |
| `MAX_VERIFS_PAR_PASSAGE` | 80 | annonces revérifiées au maximum par passage |

Pour ne plus recevoir de notifications sans arrêter le suivi, supprime le secret `NTFY_TOPIC`. Pour tout arrêter : **Actions › Suivi des ventes › ⋯ › Disable workflow**.

## Limites

- Le délai affiché est un maximum : la vente a eu lieu entre le dernier passage où l'annonce était disponible et celui où elle a été vue vendue.
- Une annonce supprimée par le vendeur n'est pas comptée comme vendue.
- Les photos des annonces expirent au bout de quelques jours ; les liens restent valables.
- Le dépôt est public mais ne contient que le code : ni recherches, ni sujet ntfy, ni annonces. La mémoire du suivi reste dans le cache interne de GitHub, et la page de rapport est publiée sans passer par les fichiers du dépôt. Elle reste lisible par quiconque connaît son adresse.
- Le cache GitHub s'efface après 7 jours sans passage : si le suivi est arrêté plus d'une semaine, il repart de zéro.
