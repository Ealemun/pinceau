"""
Client TCP vers le plugin Pinceau qui tourne dans GIMP.

Le plugin écrit, à son démarrage, un fichier `pinceau-server.json` dans le
profil utilisateur de GIMP avec {"port": ..., "token": ...}. Ce module le
retrouve (ou lit la variable d'environnement PINCEAU_CONFIG), puis envoie
une requête JSON par connexion et renvoie le résultat.
"""

import glob
import json
import os
import socket
import sys

# Délai max d'une opération (un filtre lourd ou un snapshot 4K peut être long)
TIMEOUT_SECONDS = float(os.environ.get("PINCEAU_TIMEOUT", "180"))


class PinceauError(RuntimeError):
    """Erreur renvoyée par le plugin (ou impossibilité de le joindre)."""


def _candidate_config_paths():
    """Emplacements possibles de pinceau-server.json selon l'OS et le packaging."""
    env = os.environ.get("PINCEAU_CONFIG")
    if env:
        return [env]
    home = os.path.expanduser("~")
    roots = []
    if sys.platform == "win32":
        roots.append(os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")), "GIMP"))
    elif sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Application Support", "GIMP"))
    else:
        roots.append(os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "GIMP"))
        roots.append(os.path.join(home, ".var", "app", "org.gimp.GIMP", "config", "GIMP"))   # Flatpak
        roots.append(os.path.join(home, "snap", "gimp", "current", ".config", "GIMP"))       # Snap
    paths = []
    for root in roots:
        paths += glob.glob(os.path.join(root, "3.*", "pinceau-server.json"))
    return paths


def load_config():
    """Lit le fichier de config le plus récent (port + token)."""
    paths = _candidate_config_paths()
    if not paths:
        raise PinceauError(
            "Fichier pinceau-server.json introuvable : GIMP est-il ouvert et le serveur "
            "démarré (menu Outils > Démarrer le serveur Pinceau) ? Si GIMP est installé "
            "à un emplacement inhabituel, définis PINCEAU_CONFIG vers ce fichier."
        )
    newest = max(paths, key=os.path.getmtime)
    with open(newest, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["path"] = newest
    return cfg


def call(cmd, **params):
    """Envoie une commande au plugin et renvoie son résultat (ou lève PinceauError)."""
    cfg = load_config()
    request = {"token": cfg["token"], "cmd": cmd, "params": params}
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", int(cfg["port"])), timeout=TIMEOUT_SECONDS) as sock:
            sock.sendall(payload)
            chunks = []
            while True:  # on lit jusqu'au saut de ligne final (fin de la réponse)
                chunk = sock.recv(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
    except ConnectionRefusedError as exc:
        raise PinceauError(
            f"Connexion refusée sur 127.0.0.1:{cfg['port']} : le serveur Pinceau n'est plus "
            "actif dans GIMP (relance-le via Outils > Démarrer le serveur Pinceau)."
        ) from exc
    except socket.timeout as exc:
        raise PinceauError(f"Délai dépassé ({TIMEOUT_SECONDS:.0f}s) pour la commande {cmd}") from exc

    raw = b"".join(chunks)
    if not raw:
        raise PinceauError("réponse vide du plugin")
    response = json.loads(raw.decode("utf-8"))
    if not response.get("ok"):
        message = response.get("error", "erreur inconnue")
        tb = response.get("traceback")
        # La trace complète aide à corriger, mais on la garde courte
        if tb:
            message += "\n" + "\n".join(tb.strip().splitlines()[-4:])
        raise PinceauError(message)
    return response.get("result")
