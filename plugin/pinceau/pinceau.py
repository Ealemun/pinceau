#!/usr/bin/env python3
"""
Pinceau — plugin GIMP 3.2 qui expose un petit serveur JSON local.

Rôle de ce fichier : la "plomberie".
  - enregistrer le plugin dans GIMP (menu Outils > Démarrer le serveur Pinceau) ;
  - ouvrir un socket TCP sur 127.0.0.1 (jamais exposé sur le réseau) ;
  - protéger chaque requête par un token aléatoire écrit dans le profil GIMP ;
  - lire une requête JSON par connexion, l'exécuter SUR la boucle principale
    GLib (l'API GIMP n'est pas thread-safe), renvoyer la réponse JSON, fermer.

Les opérations elles-mêmes (calques, sélections, filtres...) vivent dans
pinceau_ops.py, à côté de ce fichier.

Protocole (une ligne JSON par sens, une connexion par requête) :
  requête  : {"token": "...", "cmd": "layer_create", "params": {...}}
  réponse  : {"ok": true, "result": {...}}  ou  {"ok": false, "error": "...", "traceback": "..."}
"""

import json
import os
import secrets
import sys
import traceback

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
gi.require_version("Gio", "2.0")
from gi.repository import Gimp, Gio, GLib, GObject  # noqa: E402

# Le dossier du plugin est ajouté au chemin Python pour importer pinceau_ops.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pinceau_ops  # noqa: E402

PROC_NAME = "plug-in-pinceau-server"   # nom de la procédure enregistrée dans le PDB
DEFAULT_PORT = 9879                     # port d'écoute (loopback uniquement)
CONFIG_FILENAME = "pinceau-server.json"  # écrit dans Gimp.directory(), lu par le serveur MCP
MAX_REQUEST_BYTES = 50 * 1024 * 1024    # garde-fou contre une requête énorme


def config_path():
    """Chemin du fichier {port, token} dans le profil utilisateur de GIMP."""
    return os.path.join(Gimp.directory(), CONFIG_FILENAME)


class PinceauServer:
    """Serveur TCP loopback basé sur Gio (portable Windows/macOS/Linux)."""

    def __init__(self, port):
        self.port = port
        self.token = secrets.token_hex(24)  # nouveau token à chaque démarrage
        self.service = None
        self.loop = None  # GLib.MainLoop, renseigné par le plugin

    def start(self):
        """Ouvre le port, écrit le fichier de config et commence à écouter."""
        self.service = Gio.SocketService.new()
        address = Gio.InetSocketAddress.new(
            Gio.InetAddress.new_loopback(Gio.SocketFamily.IPV4), self.port
        )
        # add_address lève une GLib.Error si le port est déjà pris
        self.service.add_address(
            address, Gio.SocketType.STREAM, Gio.SocketProtocol.TCP, None
        )
        self._write_config()
        self.service.connect("incoming", self._on_incoming)
        self.service.start()

    def stop(self):
        """Arrête l'écoute, supprime le fichier de config et quitte la boucle."""
        if self.service is not None:
            self.service.stop()
            self.service.close()
        try:
            os.remove(config_path())
        except OSError:
            pass
        if self.loop is not None:
            self.loop.quit()

    def _write_config(self):
        """Écrit port + token, lisibles uniquement par l'utilisateur (POSIX)."""
        path = config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)
        if os.name == "posix":
            os.chmod(path, 0o600)

    # ---- traitement d'une connexion --------------------------------------

    def _on_incoming(self, service, connection, source_object):
        """Appelé par Gio sur la boucle principale : une requête, une réponse."""
        response = None
        try:
            istream = Gio.DataInputStream.new(connection.get_input_stream())
            raw = istream.read_line_utf8(None)
            # Selon la version de PyGObject, read_line_utf8 renvoie (str, len) ou str
            line = raw[0] if isinstance(raw, tuple) else raw
            if line is None or len(line) > MAX_REQUEST_BYTES:
                response = {"ok": False, "error": "requête vide ou trop volumineuse"}
            else:
                response = self._handle(line)
        except Exception as exc:  # noqa: BLE001 — on renvoie toujours une réponse
            response = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

        try:
            data = (json.dumps(response, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            ostream = connection.get_output_stream()
            ostream.write_all(data, None)
            ostream.flush(None)
        except Exception:  # noqa: BLE001 — le client a pu partir, on ignore
            pass
        finally:
            try:
                connection.close(None)
            except Exception:  # noqa: BLE001
                pass
        return True  # True = connexion prise en charge

    def _handle(self, line):
        """Vérifie le token puis délègue la commande à pinceau_ops."""
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"JSON invalide : {exc}"}

        if not secrets.compare_digest(str(request.get("token", "")), self.token):
            return {"ok": False, "error": "token invalide"}

        cmd = request.get("cmd")
        params = request.get("params") or {}

        # Commandes "système" gérées ici plutôt que dans les opérations
        if cmd == "ping":
            return {"ok": True, "result": {"version": pinceau_ops.VERSION,
                                           "gimp": Gimp.version()}}
        if cmd == "shutdown":
            GLib.idle_add(self.stop)  # on répond d'abord, on s'arrête ensuite
            return {"ok": True, "result": "arrêt en cours"}

        try:
            result = pinceau_ops.dispatch(cmd, params)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 — erreur d'opération renvoyée au client
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()}


class Pinceau(Gimp.PlugIn):
    """Classe de plugin GIMP 3 : déclare une procédure et son entrée de menu."""

    def do_query_procedures(self):
        return [PROC_NAME]

    def do_set_i18n(self, name):
        return False, None, None  # pas de traduction gettext (domaine, catalogue)

    def do_create_procedure(self, name):
        procedure = Gimp.Procedure.new(
            self, name, Gimp.PDBProcType.PLUGIN, self.run, None
        )
        # GIMP exige qu'une procédure placée dans le menu <Image> ait pour
        # premier argument un GimpRunMode, sinon il refuse de l'installer.
        procedure.add_enum_argument("run-mode", "Run mode", "Mode d'exécution",
                                    Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                    GObject.ParamFlags.READWRITE)
        procedure.set_menu_label("Démarrer le serveur Pinceau")
        procedure.add_menu_path("<Image>/Tools/")
        # Toujours actif, même sans image ouverte
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.ALWAYS)
        procedure.set_documentation(
            "Serveur local Pinceau (pilotage de GIMP par un client MCP)",
            "Ouvre un socket TCP sur 127.0.0.1 protégé par token et exécute "
            "les commandes reçues dans le processus GIMP.",
            name,
        )
        procedure.set_attribution("Pinceau", "Pinceau", "2026")
        return procedure

    def run(self, procedure, config, run_data):
        """Point d'entrée : démarre le serveur et bloque sur la boucle GLib."""
        port = int(os.environ.get("PINCEAU_PORT", DEFAULT_PORT))
        server = PinceauServer(port)
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001 — port occupé, droits, etc.
            Gimp.message(f"Pinceau : impossible de démarrer sur le port {port} : {exc}")
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc))
            )

        Gimp.message(f"Pinceau : serveur démarré sur 127.0.0.1:{port}")
        loop = GLib.MainLoop()
        server.loop = loop
        pinceau_ops.SERVER = server  # permet à run_python d'arrêter le serveur si besoin
        loop.run()  # bloque jusqu'à la commande "shutdown"
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


Gimp.main(Pinceau.__gtype__, sys.argv)
