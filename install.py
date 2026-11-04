#!/usr/bin/env python3
"""
Installe (ou désinstalle) le plugin Pinceau dans le dossier plug-ins de GIMP 3,
puis affiche la configuration à coller dans le client MCP.

Usage :
    python install.py              # installe
    python install.py --uninstall  # retire le plugin
    python install.py --dir CHEMIN # force le dossier plug-ins de GIMP

Le plugin doit se trouver dans <profil GIMP>/plug-ins/pinceau/pinceau.py :
GIMP n'enregistre qu'un fichier portant le même nom que son dossier, les
autres fichiers du dossier (pinceau_ops.py) sont importés par celui-ci.
"""

import argparse
import glob
import os
import shutil
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_SRC = os.path.join(HERE, "plugin", "pinceau")


def gimp_profile_dirs():
    """Profils GIMP 3.x candidats (le plus récent d'abord)."""
    home = os.path.expanduser("~")
    roots = []
    if sys.platform == "win32":
        roots.append(os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")), "GIMP"))
    elif sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Application Support", "GIMP"))
    else:
        roots.append(os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "GIMP"))
        roots.append(os.path.join(home, ".var", "app", "org.gimp.GIMP", "config", "GIMP"))  # Flatpak
        roots.append(os.path.join(home, "snap", "gimp", "current", ".config", "GIMP"))      # Snap
    found = []
    for root in roots:
        found += [d for d in glob.glob(os.path.join(root, "3.*")) if os.path.isdir(d)]
    # Tri : version la plus haute d'abord (3.2 avant 3.0)
    return sorted(found, key=lambda d: os.path.basename(d), reverse=True)


def main():
    parser = argparse.ArgumentParser(description="Installe le plugin Pinceau dans GIMP 3")
    parser.add_argument("--uninstall", action="store_true", help="retire le plugin")
    parser.add_argument("--dir", help="dossier plug-ins de GIMP (sinon détecté)")
    args = parser.parse_args()

    if args.dir:
        plugins_dir = args.dir
    else:
        profiles = gimp_profile_dirs()
        if not profiles:
            print("Aucun profil GIMP 3.x trouvé. Lance GIMP une fois, puis relance ce script,")
            print("ou indique le dossier : python install.py --dir \"<profil GIMP>/plug-ins\"")
            print("(Édition > Préférences > Dossiers > Greffons dans GIMP affiche ce chemin).")
            sys.exit(1)
        if len(profiles) > 1:
            print("Plusieurs profils GIMP trouvés, j'utilise le plus récent :", profiles[0])
        plugins_dir = os.path.join(profiles[0], "plug-ins")

    dest = os.path.join(plugins_dir, "pinceau")

    if args.uninstall:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
            print("Plugin retiré :", dest)
        else:
            print("Rien à retirer :", dest)
        return

    os.makedirs(plugins_dir, exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)  # réinstallation propre (pas de .pyc périmé)
    shutil.copytree(PLUGIN_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Sous Linux/macOS, GIMP exige que le fichier principal soit exécutable
    main_file = os.path.join(dest, "pinceau.py")
    if os.name == "posix":
        st = os.stat(main_file)
        os.chmod(main_file, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print("Plugin installé dans :", dest)
    print()
    print("Étapes suivantes :")
    print("  1. (Re)démarre GIMP.")
    print("  2. Menu Outils > « Démarrer le serveur Pinceau » (à refaire à chaque lancement de GIMP).")
    print("  3. Ajoute le serveur MCP à ton client. Entrée à ajouter dans sa configuration JSON :")
    print()
    python = sys.executable.replace("\\", "\\\\")
    server_dir = os.path.join(HERE, "server").replace("\\", "\\\\")
    print('  "pinceau": {')
    print(f'    "command": "{python}",')
    print('    "args": ["-m", "pinceau_mcp"],')
    print(f'    "env": {{"PYTHONPATH": "{server_dir}"}}')
    print("  }")
    print()
    print("  (Le paquet mcp doit être installé pour ce Python : pip install -r requirements.txt)")


if __name__ == "__main__":
    main()
