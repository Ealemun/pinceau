#!/usr/bin/env python3
"""
Installe (ou désinstalle) le plugin Pinceau dans le dossier plug-ins de GIMP 3,
puis affiche la configuration à coller dans le client MCP.

Usage :
    python install.py                          # dépendances + plugin
    python install.py --mcp-config FICHIER.json # + inscrit Pinceau dans une config MCP
    python install.py --print-entry            # affiche seulement l'entrée JSON
    python install.py --uninstall              # retire le plugin (et l'entrée MCP si --mcp-config)
    python install.py --dir CHEMIN             # force le dossier plug-ins de GIMP
    python install.py --no-pip                 # n'installe pas les dépendances Python

Le plugin doit se trouver dans <profil GIMP>/plug-ins/pinceau/pinceau.py :
GIMP n'enregistre qu'un fichier portant le même nom que son dossier, les
autres fichiers du dossier (pinceau_ops.py) sont importés par celui-ci.
"""

import argparse
import glob
import json
import os
import shutil
import stat
import subprocess
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


def mcp_entry():
    """Entrée de configuration MCP standard pour lancer le serveur Pinceau.

    On utilise le Python qui exécute ce script (sys.executable) : c'est
    celui dans lequel les dépendances viennent d'être installées.
    """
    return {
        "command": sys.executable,
        "args": ["-m", "pinceau_mcp"],
        "env": {"PYTHONPATH": os.path.join(HERE, "server")},
    }


def install_requirements():
    """pip install -r requirements.txt avec le Python courant."""
    req = os.path.join(HERE, "requirements.txt")
    print("Installation des dépendances Python...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req])
    if result.returncode != 0:
        print("Échec de pip. Relance à la main : python -m pip install -r requirements.txt")
        sys.exit(1)


def update_mcp_config(path, remove=False):
    """Ajoute (ou retire) l'entrée "pinceau" dans un fichier JSON de config MCP.

    Format attendu, commun à la plupart des clients MCP :
        {"mcpServers": {"pinceau": {...}, ...autres serveurs conservés...}}
    Le fichier est créé s'il n'existe pas ; une copie .bak est faite avant écriture.
    """
    path = os.path.expanduser(path)
    config = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"Le fichier {path} n'est pas du JSON valide ({exc}) : je n'y touche pas.")
                sys.exit(1)
        shutil.copy2(path, path + ".bak")  # sauvegarde avant modification
    servers = config.setdefault("mcpServers", {})
    if remove:
        if servers.pop("pinceau", None) is None:
            print("Aucune entrée pinceau dans", path)
            return
    else:
        servers["pinceau"] = mcp_entry()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(("Entrée pinceau retirée de : " if remove else "Entrée pinceau écrite dans : ") + path)


def main():
    parser = argparse.ArgumentParser(description="Installe le plugin Pinceau dans GIMP 3")
    parser.add_argument("--uninstall", action="store_true", help="retire le plugin")
    parser.add_argument("--dir", help="dossier plug-ins de GIMP (sinon détecté)")
    parser.add_argument("--mcp-config", action="append", default=[], metavar="FICHIER",
                        help="fichier JSON de config MCP où inscrire Pinceau (option répétable)")
    parser.add_argument("--print-entry", action="store_true",
                        help="affiche l'entrée JSON du serveur MCP et s'arrête")
    parser.add_argument("--no-pip", action="store_true", help="n'installe pas les dépendances Python")
    args = parser.parse_args()

    if args.print_entry:
        print(json.dumps({"pinceau": mcp_entry()}, indent=2, ensure_ascii=False))
        return

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
        for cfg in args.mcp_config:
            update_mcp_config(cfg, remove=True)
        return

    if not args.no_pip:
        install_requirements()

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

    for cfg in args.mcp_config:
        update_mcp_config(cfg)

    print()
    print("Étapes suivantes :")
    print("  1. (Re)démarre GIMP.")
    print("  2. Menu Outils > « Démarrer le serveur Pinceau » (à refaire à chaque lancement de GIMP).")
    if args.mcp_config:
        print("  3. Redémarre ton client MCP pour qu'il charge Pinceau.")
    else:
        print("  3. Inscris le serveur dans ton client MCP : relance avec --mcp-config <fichier.json>,")
        print("     ou ajoute à la main cette entrée sous \"mcpServers\" :")
        print()
        print(json.dumps({"pinceau": mcp_entry()}, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
