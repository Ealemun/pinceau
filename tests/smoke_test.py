#!/usr/bin/env python3
"""
Test de fumée : enchaîne les opérations principales contre un VRAI GIMP.

Prérequis : GIMP ouvert et serveur Pinceau démarré (Outils > Démarrer le serveur Pinceau).
Usage : python tests/smoke_test.py

Le test crée une image, y dessine, applique filtres et ajustements, fait un
photomontage, puis écrit les aperçus et exports dans tests/output/.
Chaque étape affiche OK ou l'erreur renvoyée par le plugin ; le test continue
pour donner une vue d'ensemble, et termine avec un bilan.
"""

import base64
import os
import sys
import traceback

# Permet d'importer pinceau_mcp sans installation
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
from pinceau_mcp import bridge  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUT, exist_ok=True)

results = []  # (nom, ok, message)


def step(name, fn):
    """Exécute une étape, enregistre le résultat, continue en cas d'erreur."""
    try:
        value = fn()
        results.append((name, True, ""))
        print(f"OK   {name}")
        return value
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, str(exc)))
        print(f"FAIL {name}\n     {str(exc).splitlines()[0]}")
        return None


def save_snapshot(name, **kw):
    """Sauvegarde un aperçu dans tests/output pour contrôle visuel."""
    kw.setdefault("max_size", 800)
    data = bridge.call("snapshot", **kw)
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data["png_base64"]))
    return path


def main():
    status = step("status", lambda: bridge.call("status"))
    if status is None:
        print("\nImpossible de joindre le plugin : GIMP est-il ouvert et le serveur démarré ?")
        sys.exit(1)
    print("     GIMP", status["gimp"], "— Pinceau", status["pinceau"])

    # --- Image et calques ------------------------------------------------
    img = step("image_new", lambda: bridge.call("image_new", width=800, height=600, fill="#1e1e2e"))
    image_id = img["id"]

    step("gradient", lambda: bridge.call("gradient", image_id=image_id, x1=0, y1=0, x2=800, y2=600,
                                         **{"from": "#1e1e2e", "to": "#4a3a8a"}, shape="linear"))
    step("layer_create", lambda: bridge.call("layer_create", image_id=image_id, name="Formes"))
    step("select ellipse", lambda: bridge.call("select", image_id=image_id, shape="ellipse",
                                               x=200, y=150, width=300, height=300, feather=0))
    step("fill", lambda: bridge.call("fill", image_id=image_id, layer_name="Formes", color="#ff9f43"))
    step("select none", lambda: bridge.call("select", image_id=image_id, shape="none"))
    step("filter gaussian-blur", lambda: bridge.call("filter_apply", image_id=image_id, layer_name="Formes",
                                                     name="gegl:gaussian-blur",
                                                     params={"std-dev-x": 6.0, "std-dev-y": 6.0}))
    step("paint", lambda: bridge.call("paint", image_id=image_id, layer_name="Formes", tool="paintbrush",
                                      color="#ffffff", size=25, hardness=0.8,
                                      points=[[100, 500], [300, 420], [500, 520], [700, 450]]))
    step("text_add", lambda: bridge.call("text_add", image_id=image_id, text="Pinceau", size=72,
                                         color="#ffffff", x=250, y=40))
    step("layer_set mode", lambda: bridge.call("layer_set", image_id=image_id, layer_name="Formes",
                                               opacity=85, mode="screen"))
    step("adjust hue_saturation", lambda: bridge.call("adjust", image_id=image_id, layer_name="Formes",
                                                      kind="hue_saturation", hue=30, saturation=20))
    step("mask add", lambda: bridge.call("mask", image_id=image_id, layer_name="Formes", action="add", **{"from": "white"}))
    step("gradient on mask", lambda: bridge.call("gradient", image_id=image_id, layer_name="Formes", target="mask",
                                                 x1=0, y1=0, x2=800, y2=0, **{"from": "#ffffff", "to": "#000000"}))
    step("group_create", lambda: bridge.call("group_create", image_id=image_id, name="Décor"))
    step("path_create", lambda: bridge.call("path_create", image_id=image_id, name="Trait",
                                            points=[[50, 50], [750, 50, 400, 0, 500, 120]]))
    path = bridge.call("image_info", image_id=image_id)["paths"]
    step("path_stroke", lambda: bridge.call("path_stroke", image_id=image_id, layer_name="Formes",
                                            path_id=path[0]["id"], color="#00d2ff", width=4))
    step("pick_color", lambda: bridge.call("pick_color", image_id=image_id, x=350, y=300))
    step("histogram", lambda: bridge.call("histogram", image_id=image_id, layer_name="Formes"))
    step("filter_search", lambda: bridge.call("filter_search", query="shadow"))

    # Filtre masqué par GIMP : doit être remplacé automatiquement par gimp:threshold
    def masked_filter_replaced():
        bridge.call("layer_duplicate", image_id=image_id, layer_name="Formes", name="Seuil")
        res = bridge.call("filter_apply", image_id=image_id, layer_name="Seuil", name="gegl:threshold")
        assert res["filter"] == "gimp:threshold", res["filter"]
        bridge.call("layer_delete", image_id=image_id, layer_name="Seuil")
    step("filtre masqué remplacé", masked_filter_replaced)

    # Filtre masqué sans équivalent : doit renvoyer une erreur claire, sans fenêtre dans GIMP
    def masked_filter_refused():
        try:
            bridge.call("filter_apply", image_id=image_id, layer_name="Formes", name="gegl:linear-gradient")
        except bridge.PinceauError as exc:
            assert "gimp_gradient" in str(exc), str(exc)
            return
        raise AssertionError("le filtre masqué aurait dû être refusé")
    step("filtre masqué refusé proprement", masked_filter_refused)
    step("filter_describe gimp:levels", lambda: bridge.call("filter_describe", image_id=image_id, name="gimp:levels"))
    step("filter_describe", lambda: bridge.call("filter_describe", name="gegl:dropshadow"))
    step("pdb_search", lambda: bridge.call("pdb_search", query="gimp-image-get-width"))
    step("pdb_call", lambda: bridge.call("pdb_call", name="gimp-image-get-width", args={"image": image_id}))
    step("run_python", lambda: bridge.call("run_python", code="result = image().get_width() * 2"))

    # --- Photomontage : on exporte un calque puis on le replace ----------
    export_png = os.path.join(OUT, "export.png")
    step("image_export png", lambda: bridge.call("image_export", image_id=image_id, path=export_png))
    step("image_place", lambda: bridge.call("image_place", image_id=image_id, path=export_png, name="Montage",
                                            width=300, x=480, y=320, opacity=90))
    step("layer_transform rotate", lambda: bridge.call("layer_transform", image_id=image_id, layer_name="Montage",
                                                       kind="rotate", degrees=-12))
    step("layer_align", lambda: bridge.call("layer_align", image_id=image_id, layer_name="Montage",
                                            horizontal="right", vertical="bottom", dx=-20, dy=-20))
    step("select contiguous", lambda: bridge.call("select", image_id=image_id, layer_name="Fond", shape="contiguous",
                                                  x=5, y=5, threshold=0.2))
    step("select none 2", lambda: bridge.call("select", image_id=image_id, shape="none"))

    # --- Nouveautés 0.3 : moteur de dessin, tailles par point, échelle, brosses
    step("paint sizes", lambda: bridge.call("paint", image_id=image_id, layer_name="Formes", color="#202020",
                                            points=[[100, 80], [140, 70], [180, 66], [220, 70], [260, 80]],
                                            sizes=[2, 8, 14, 8, 2], hardness=0.9))
    step("burn", lambda: bridge.call("paint", image_id=image_id, layer_name="Fond", tool="burn",
                                     points=[[700, 80], [760, 120]], size=40, exposure=30, range="shadows"))
    step("dessin_aide", lambda: bridge.call("dessin_aide"))

    def moteur_dessin():
        bridge.call("layer_create", image_id=image_id, name="Construction")
        res = bridge.call("dessin", image_id=image_id, layer_name="Construction", ops=[
            {"fn": "camera", "args": {"cx": 650, "cy": 200, "rotation": 35, "plongee": 15}},
            {"fn": "ellipsoide", "args": {"nom": "crane", "centre": [0, 0, 0], "rayons": [60, 50, 55]},
             "as": "tete"},
            {"fn": "dessiner", "args": {"vol": "$tete", "size": 2.0}},
        ])
        assert res["executes"] == 3 and "tete" in res["variables"], res
    step("dessin (moteur préchargé)", moteur_dessin)
    step("snapshot scale 2", lambda: save_snapshot("snapshot_x2.png", image_id=image_id, scale=2,
                                                   region={"x": 560, "y": 120, "width": 180, "height": 160}))

    def brosses():
        info = bridge.call("image_info", image_id=image_id)
        assert "brosses" in info, info.keys()
        print("     brosses de matière :", len(info["brosses"]["texturees"]),
              "| craie :", info["brosses"]["craie"], "| fusain :", info["brosses"]["fusain"])
    step("image_info brosses", brosses)

    # --- Vision et sauvegarde --------------------------------------------
    step("snapshot composite", lambda: save_snapshot("snapshot.png", image_id=image_id))
    step("snapshot layer", lambda: save_snapshot("snapshot_formes.png", image_id=image_id, layer_name="Formes"))
    step("snapshot region", lambda: save_snapshot("snapshot_region.png", image_id=image_id,
                                                  region={"x": 150, "y": 100, "width": 400, "height": 400}))
    step("save_xcf", lambda: bridge.call("image_save_xcf", image_id=image_id, path=os.path.join(OUT, "test.xcf")))
    step("image_info", lambda: bridge.call("image_info", image_id=image_id))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} étapes réussies. Aperçus dans {OUT}")
    if failed:
        print("Échecs :")
        for name, _, msg in failed:
            print(f"  - {name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except bridge.PinceauError as exc:
        print("Erreur :", exc)
        sys.exit(1)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
