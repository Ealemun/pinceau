# Pinceau — piloter GIMP 3.2 depuis un client MCP

Pinceau expose GIMP à un assistant via le protocole MCP : calques, groupes, masques,
sélections (rectangle, ellipse, lasso, baguette magique, par couleur, chemin), dessin
(pinceau, crayon, aérographe, gomme, étalement), remplissages et dégradés, texte,
ajustements de couleur (niveaux, courbes, teinte/saturation, balance, colorisation...),
tous les filtres GEGL, photomontage (insertion d'images comme calques, transformations,
perspective, alignement), et un retour visuel par aperçu PNG. Rien n'est généré :
l'assistant travaille comme un graphiste, avec les outils de GIMP.

Pour tout ce qui n'a pas d'outil dédié, trois voies de secours : recherche et appel
de n'importe quelle procédure du PDB, recherche et application de n'importe quelle
opération GEGL, et exécution de Python directement dans GIMP.

## Architecture

```
Client MCP ──stdio──▶ server/pinceau_mcp (41 outils, Python + SDK mcp)
                              │  JSON sur TCP 127.0.0.1:9879, token aléatoire
                              ▼
                     plugin/pinceau (plugin GIMP 3, PyGObject)
                              ▼
                            GIMP 3.2
```

- Le plugin n'écoute que sur l'interface locale et vérifie un token régénéré à chaque
  démarrage (fichier `pinceau-server.json` dans le profil GIMP, lisible par l'utilisateur seul).
- Les commandes s'exécutent sur la boucle principale GLib, jamais dans un thread.
- Chaque appel d'outil est un groupe d'annulation : un Ctrl+Z dans GIMP annule l'appel entier.
- Aucune dépendance côté plugin (le Python embarqué de GIMP suffit).

## Prérequis

- GIMP **3.2 ou plus récent** (`Aide > À propos`). GIMP 2.10 n'est pas supporté : son API
  Python (Python-Fu / Python 2) est incompatible.
- Python 3.10+ sur la machine pour le serveur MCP, avec `pip install -r requirements.txt`.

## Installation

```bash
git clone https://github.com/Ealemun/pinceau
cd pinceau
python install.py --mcp-config "<chemin du fichier de config JSON de ton client MCP>"
```

Cette seule commande :

1. installe les dépendances Python (`requirements.txt`) dans le Python qui l'exécute ;
2. copie le plugin dans `<profil GIMP>/plug-ins/pinceau/` (Windows : `%APPDATA%\GIMP\3.x\plug-ins`,
   Linux : `~/.config/GIMP/3.x/plug-ins`, macOS : `~/Library/Application Support/GIMP/3.x/plug-ins`) ;
3. ajoute l'entrée `pinceau` sous `"mcpServers"` dans le fichier de config indiqué, sans toucher
   aux autres serveurs (une copie `.bak` est faite avant). `--mcp-config` est répétable pour
   inscrire Pinceau dans plusieurs clients.

Sans `--mcp-config`, le script affiche l'entrée JSON à coller à la main (`--print-entry` l'affiche
seule). Pour un client qui s'inscrit par ligne de commande plutôt que par fichier, l'entrée donne
la commande à lancer : `<python> -m pinceau_mcp` avec `PYTHONPATH=<dossier pinceau>/server`.

Ensuite :

1. (Re)démarre GIMP.
2. Menu **Outils > Démarrer le serveur Pinceau**. Un message confirme le port. À refaire à
   chaque lancement de GIMP.
3. Redémarre le client MCP.

Désinstallation : `python install.py --uninstall --mcp-config <même fichier>`.

Options : `--dir` force le dossier plug-ins de GIMP, `--no-pip` saute l'installation des dépendances.

## Vérifier l'installation

GIMP ouvert et serveur démarré :

```bash
python tests/smoke_test.py
```

Le test crée une image, enchaîne une trentaine d'opérations (dégradé, sélection, filtre,
pinceau, texte, masque, chemin, photomontage, PDB, Python) et dépose les aperçus dans
`tests/output/`. Il affiche OK/FAIL par étape et continue en cas d'erreur.

## Les outils

| Couche | Outils |
|---|---|
| Session et vision | `gimp_status`, `gimp_image_info`, `gimp_set_current_image`, `gimp_snapshot` (image, calque ou région), `gimp_pick_color`, `gimp_histogram` |
| Fichiers | `gimp_image_new`, `gimp_image_open`, `gimp_image_place` (photomontage), `gimp_image_save_xcf`, `gimp_image_export`, `gimp_image_close`, `gimp_image_transform` (scale, canevas, crop, rotate, flip, flatten, merge) |
| Calques | `gimp_layer_create`, `gimp_group_create`, `gimp_layer_set`, `gimp_layer_edit` (duplicate, delete, merge_down, move), `gimp_layer_transform` (scale, rotate, flip, perspective, shear), `gimp_layer_align`, `gimp_mask` |
| Sélections et chemins | `gimp_select` (rectangle, ellipse, polygon, contiguous, color, path, layer_alpha...), `gimp_selection_modify`, `gimp_selection_channel`, `gimp_path_create`, `gimp_stroke` |
| Peinture et texte | `gimp_fill`, `gimp_bucket_fill`, `gimp_gradient`, `gimp_paint` (paintbrush, pencil, airbrush, eraser, smudge, dodge, burn, clone), `gimp_text`, `gimp_text_edit`, `gimp_resources` |
| Couleur et filtres | `gimp_adjust` (12 ajustements), `gimp_filter_search`, `gimp_filter_describe`, `gimp_filter_apply` (destructif ou non), `gimp_layer_effects` |
| Accès générique | `gimp_pdb_search`, `gimp_pdb_describe`, `gimp_pdb_call`, `gimp_run_python` |

Conventions : `image_id` optionnel (image courante par défaut), `layer_id` ou `layer_name`
optionnels (calque sélectionné par défaut), `target="mask"` pour agir sur le masque d'un calque,
couleurs en `#rrggbb` / `#rrggbbaa` ou `[r, g, b, a]`, coordonnées en pixels.

## Dépannage

- *« Fichier pinceau-server.json introuvable »* : le serveur n'est pas démarré dans GIMP, ou
  GIMP utilise un profil inhabituel. Définis `PINCEAU_CONFIG` vers le fichier (son dossier est
  indiqué dans `Édition > Préférences > Dossiers`).
- *« Connexion refusée »* : GIMP a été fermé ou le plugin s'est arrêté ; relance-le via le menu.
- *Le menu n'apparaît pas* : vérifie que le dossier s'appelle `pinceau` et contient `pinceau.py`
  (sous Linux/macOS, exécutable). Lance GIMP depuis un terminal pour voir les erreurs d'import.
- *Port 9879 déjà pris* : lance GIMP avec la variable `PINCEAU_PORT=9880` (le serveur MCP lit
  le port dans le fichier de config, rien d'autre à changer).
- Annulation : GIMP 3.2 n'offre pas d'annulation programmatique aux plugins ; utilise Ctrl+Z
  dans GIMP (chaque appel d'outil = une étape).

## Structure

```
plugin/pinceau/pinceau.py      transport : socket Gio, token, boucle GLib, enregistrement GIMP
plugin/pinceau/pinceau_ops.py  opérations (une fonction op_* par commande)
server/pinceau_mcp/bridge.py   client TCP, lecture de la config
server/pinceau_mcp/server.py   outils MCP typés
install.py                     installation du plugin
tests/smoke_test.py            test de bout en bout contre un vrai GIMP
```
