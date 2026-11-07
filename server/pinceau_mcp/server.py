"""
Pinceau — serveur MCP (stdio) qui expose GIMP à un client MCP.

Quatre couches d'outils :
  1. Python brut (`gimp_run_python`) : couverture totale de l'API GIMP 3 ;
  2. découverte (`gimp_pdb_*`, `gimp_filter_search/describe`) : interroger
     l'API réellement installée au lieu de deviner des signatures ;
  3. outils typés de haut niveau (calques, sélections, peinture, couleur...) ;
  4. vision (`gimp_snapshot`, `gimp_pick_color`, `gimp_histogram`) : la boucle
     « éditer → regarder → corriger ».

Chaque outil appelle le plugin via bridge.call(...). Les erreurs du plugin
remontent au client sous forme lisible.

Conventions communes à tous les outils :
  - image_id : optionnel, image courante par défaut (celle du dernier
    image_new / image_open / set_current_image, sinon la première ouverte) ;
  - layer_id / layer_name : optionnels, calque sélectionné dans GIMP par défaut ;
  - couleurs : "#rrggbb", "#rrggbbaa" ou [r, g, b(, a)] en 0..255 ;
  - coordonnées en pixels, origine en haut à gauche de l'image.
"""

import base64
import json
from typing import Any, Optional

# Le SDK mcp 2.x a renommé FastMCP en MCPServer ; on supporte les deux versions.
try:
    from mcp.server.mcpserver import Image, MCPServer as FastMCP  # mcp >= 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

from . import bridge

mcp = FastMCP(
    "pinceau",
    instructions=(
        "Pinceau pilote GIMP 3.2 : calques, masques, sélections, chemins, peinture, "
        "texte, ajustements, filtres GEGL, photomontage. Commence par gimp_status puis "
        "gimp_image_info ; après chaque étape visuelle importante, appelle gimp_snapshot "
        "pour vérifier le résultat avant de continuer. Un appel d'outil = une étape "
        "d'annulation (Ctrl+Z) dans GIMP. Pour tout ce que les outils typés ne couvrent "
        "pas, utilise gimp_filter_search/gimp_pdb_search puis gimp_filter_apply/gimp_pdb_call, "
        "ou gimp_run_python en dernier recours."
    ),
)


def _j(result: Any) -> str:
    """Sérialise un résultat du plugin pour le client MCP."""
    return json.dumps(result, ensure_ascii=False, indent=1)


def _clean(params: dict) -> dict:
    """Retire les paramètres None pour laisser le plugin appliquer ses défauts."""
    return {k: v for k, v in params.items() if v is not None}


# =============================================================================
# 0. Session et vision
# =============================================================================

@mcp.tool()
def gimp_status() -> str:
    """État de GIMP : version, images ouvertes, image courante, couleurs et brosse du contexte.
    À appeler en premier pour vérifier que le serveur Pinceau tourne dans GIMP."""
    return _j(bridge.call("status"))


@mcp.tool()
def gimp_image_info(image_id: Optional[int] = None) -> str:
    """Description complète d'une image : taille, sélection, calques (arbre avec id, nom, type,
    position, opacité, mode, visibilité, masque), chemins. Les ids servent aux autres outils."""
    return _j(bridge.call("image_info", **_clean({"image_id": image_id})))


@mcp.tool()
def gimp_set_current_image(image_id: int) -> str:
    """Définit l'image courante utilisée par défaut par tous les autres outils."""
    return _j(bridge.call("set_current_image", image_id=image_id))


@mcp.tool()
def gimp_snapshot(image_id: Optional[int] = None, layer_id: Optional[int] = None,
                  layer_name: Optional[str] = None, max_size: int = 1024,
                  region: Optional[dict] = None, scale: Optional[float] = None) -> Image:
    """Aperçu PNG de l'image composite (par défaut), d'un seul calque (layer_id/layer_name)
    ou d'une région {"x","y","width","height"}. Par défaut réduit à max_size px de côté.
    scale donne une échelle exacte à la place : 1 = pixels réels, 2 ou 3 = agrandi au plus
    proche voisin (pixels nets), idéal pour contrôler yeux, jonctions et bords sur une région ;
    un point (u, v) de l'aperçu correspond alors à (x + u/scale, y + v/scale) dans l'image.
    Utilise-le après chaque étape importante pour contrôler visuellement le résultat."""
    result = bridge.call("snapshot", **_clean({"image_id": image_id, "layer_id": layer_id,
                                                 "layer_name": layer_name, "max_size": max_size,
                                                 "region": region, "scale": scale}))
    return Image(data=base64.b64decode(result["png_base64"]), format="png")


@mcp.tool()
def gimp_pick_color(x: float, y: float, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                    sample_merged: bool = True, radius: float = 0.0) -> str:
    """Couleur du pixel (x, y) : hex et rgba. sample_merged=True lit l'image composite,
    False lit uniquement le calque ; radius > 0 moyenne les pixels voisins."""
    return _j(bridge.call("pick_color", **_clean({"x": x, "y": y, "image_id": image_id, "layer_id": layer_id,
                                                   "sample_merged": sample_merged, "average": radius > 0,
                                                   "radius": radius})))


@mcp.tool()
def gimp_histogram(image_id: Optional[int] = None, layer_id: Optional[int] = None,
                   channel: str = "value") -> str:
    """Statistiques d'histogramme d'un calque (mean, median, std_dev, percentile).
    channel : value, red, green, blue, alpha, luminance."""
    return _j(bridge.call("histogram", **_clean({"image_id": image_id, "layer_id": layer_id, "channel": channel})))


# =============================================================================
# 1. Fichiers / image
# =============================================================================

@mcp.tool()
def gimp_image_new(width: int = 1920, height: int = 1080, fill: Optional[str] = None,
                   layer_name: str = "Fond") -> str:
    """Crée une nouvelle image RVB avec un calque de fond (transparent, ou rempli de `fill`).
    Elle devient l'image courante et s'ouvre dans GIMP."""
    return _j(bridge.call("image_new", **_clean({"width": width, "height": height, "fill": fill,
                                                  "layer_name": layer_name})))


@mcp.tool()
def gimp_image_open(path: str) -> str:
    """Ouvre un fichier (PNG, JPEG, XCF, PSD, WebP, TIFF, SVG...) comme nouvelle image courante."""
    return _j(bridge.call("image_open", path=path))


@mcp.tool()
def gimp_image_place(path: str, image_id: Optional[int] = None, name: Optional[str] = None,
                     x: int = 0, y: int = 0, scale: Optional[float] = None,
                     width: Optional[int] = None, height: Optional[int] = None,
                     opacity: Optional[float] = None, mode: Optional[str] = None,
                     parent_id: Optional[int] = None, position: int = 0) -> str:
    """Photomontage : insère un fichier image comme nouveau calque dans l'image courante,
    avec position (x, y), redimensionnement (scale, ou width/height en gardant le ratio
    si un seul est donné), opacité (0-100), mode de fusion et groupe parent optionnel."""
    return _j(bridge.call("image_place", **_clean({"path": path, "image_id": image_id, "name": name, "x": x, "y": y,
                                                    "scale": scale, "width": width, "height": height,
                                                    "opacity": opacity, "mode": mode, "parent_id": parent_id,
                                                    "position": position})))


@mcp.tool()
def gimp_image_save_xcf(path: str, image_id: Optional[int] = None) -> str:
    """Sauvegarde le projet complet (calques, masques, chemins) au format XCF de GIMP."""
    return _j(bridge.call("image_save_xcf", **_clean({"path": path, "image_id": image_id})))


@mcp.tool()
def gimp_image_export(path: str, image_id: Optional[int] = None, max_size: Optional[int] = None) -> str:
    """Exporte l'image aplatie ; le format vient de l'extension (.png, .jpg, .webp, .tiff, .bmp).
    max_size (PNG uniquement) exporte une version réduite sans toucher à l'image."""
    return _j(bridge.call("image_export", **_clean({"path": path, "image_id": image_id, "max_size": max_size})))


@mcp.tool()
def gimp_image_close(image_id: int) -> str:
    """Ferme une image sans sauvegarder. GIMP peut refuser si une fenêtre l'affiche."""
    return _j(bridge.call("image_close", image_id=image_id))


@mcp.tool()
def gimp_image_transform(kind: str, image_id: Optional[int] = None, width: Optional[int] = None,
                         height: Optional[int] = None, x: int = 0, y: int = 0, degrees: int = 90,
                         orientation: str = "horizontal", resize_layers: bool = False,
                         merge_type: str = "expand_as_necessary") -> str:
    """Opérations sur l'image entière. kind :
    - scale (width, height) : redimensionne les pixels ;
    - resize_canvas (width, height, x, y = décalage des calques ; resize_layers) ;
    - crop (width, height, x, y ; sans dimensions : recadre à la sélection) ;
    - rotate (degrees 90/180/270) ; flip (orientation horizontal|vertical) ;
    - flatten (aplatit tout, supprime la transparence) ; merge_visible (fusionne les calques visibles)."""
    common = {"image_id": image_id}
    if kind == "scale":
        return _j(bridge.call("image_scale", **_clean({**common, "width": width, "height": height})))
    if kind == "resize_canvas":
        return _j(bridge.call("image_resize_canvas", **_clean({**common, "width": width, "height": height,
                                                                "offset_x": x, "offset_y": y, "resize_layers": resize_layers})))
    if kind == "crop":
        return _j(bridge.call("image_crop", **_clean({**common, "width": width, "height": height, "x": x, "y": y})))
    if kind == "rotate":
        return _j(bridge.call("image_rotate", **_clean({**common, "degrees": degrees})))
    if kind == "flip":
        return _j(bridge.call("image_flip", **_clean({**common, "orientation": orientation})))
    if kind == "flatten":
        return _j(bridge.call("image_flatten", **_clean(common)))
    if kind == "merge_visible":
        return _j(bridge.call("image_merge_visible", **_clean({**common, "merge_type": merge_type})))
    return "kind inconnu : scale, resize_canvas, crop, rotate, flip, flatten, merge_visible"


# =============================================================================
# 2. Calques, groupes, masques
# =============================================================================

@mcp.tool()
def gimp_layer_create(name: str = "Calque", image_id: Optional[int] = None, fill: Optional[str] = None,
                      width: Optional[int] = None, height: Optional[int] = None, x: int = 0, y: int = 0,
                      opacity: float = 100.0, mode: str = "normal", parent_id: Optional[int] = None,
                      position: int = 0) -> str:
    """Nouveau calque RVBA (transparent, ou rempli de `fill`). Taille de l'image par défaut.
    position 0 = au-dessus de la pile (ou du groupe parent_id). Il devient le calque sélectionné.
    Modes utiles : normal, multiply, screen, overlay, soft-light, hard-light, addition,
    darken-only, lighten-only, difference, hsv-hue, hsl-color, luminance."""
    return _j(bridge.call("layer_create", **_clean({"name": name, "image_id": image_id, "fill": fill, "width": width,
                                                     "height": height, "x": x, "y": y, "opacity": opacity, "mode": mode,
                                                     "parent_id": parent_id, "position": position})))


@mcp.tool()
def gimp_group_create(name: str = "Groupe", image_id: Optional[int] = None, layer_ids: Optional[list] = None,
                      parent_id: Optional[int] = None, position: int = 0) -> str:
    """Nouveau groupe de calques, vide ou contenant les calques `layer_ids` (déplacés dedans)."""
    return _j(bridge.call("group_create", **_clean({"name": name, "image_id": image_id, "layer_ids": layer_ids,
                                                     "parent_id": parent_id, "position": position})))


@mcp.tool()
def gimp_layer_set(image_id: Optional[int] = None, layer_id: Optional[int] = None, layer_name: Optional[str] = None,
                   name: Optional[str] = None, opacity: Optional[float] = None, mode: Optional[str] = None,
                   visible: Optional[bool] = None, lock_alpha: Optional[bool] = None,
                   x: Optional[int] = None, y: Optional[int] = None, add_alpha: Optional[bool] = None) -> str:
    """Modifie les propriétés d'un calque : nom, opacité (0-100), mode de fusion, visibilité,
    verrou alpha (peindre sans déborder des pixels existants), position (x, y), ajout d'un canal alpha.
    Le calque devient le calque sélectionné (cible par défaut des outils suivants)."""
    return _j(bridge.call("layer_set", **_clean({"image_id": image_id, "layer_id": layer_id, "layer_name": layer_name,
                                                  "name": name, "opacity": opacity, "mode": mode, "visible": visible,
                                                  "lock_alpha": lock_alpha, "x": x, "y": y, "add_alpha": add_alpha})))


@mcp.tool()
def gimp_layer_edit(action: str, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                    layer_name: Optional[str] = None, name: Optional[str] = None, position: Optional[int] = None,
                    parent_id: Optional[int] = None, to_root: bool = False) -> str:
    """Actions de pile sur un calque. action :
    - duplicate (name optionnel) ; delete ; merge_down (fusionne avec le calque du dessous) ;
    - move (position dans la pile ; parent_id pour entrer dans un groupe ; to_root=True pour en sortir) ;
    - resize_to_image (étend le calque aux dimensions de l'image)."""
    target = {"image_id": image_id, "layer_id": layer_id, "layer_name": layer_name}
    if action == "duplicate":
        return _j(bridge.call("layer_duplicate", **_clean({**target, "name": name})))
    if action == "delete":
        return _j(bridge.call("layer_delete", **_clean(target)))
    if action == "merge_down":
        return _j(bridge.call("layer_merge_down", **_clean(target)))
    if action == "resize_to_image":
        return _j(bridge.call("layer_resize_to_image", **_clean(target)))
    if action == "move":
        params = _clean({**target, "position": position if position is not None else 0})
        if to_root:
            params["parent_id"] = None  # clé présente et None = racine
        elif parent_id is not None:
            params["parent_id"] = parent_id
        return _j(bridge.call("layer_move", **params))
    return "action inconnue : duplicate, delete, merge_down, move, resize_to_image"


@mcp.tool()
def gimp_layer_transform(kind: str, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                         layer_name: Optional[str] = None, factor: Optional[float] = None,
                         width: Optional[int] = None, height: Optional[int] = None, degrees: Optional[float] = None,
                         center_x: Optional[float] = None, center_y: Optional[float] = None,
                         orientation: str = "horizontal", corners: Optional[list] = None,
                         dx: int = 0, dy: int = 0, amount: Optional[float] = None,
                         interpolation: str = "cubic", clip: str = "adjust") -> str:
    """Transformation géométrique d'un calque. kind :
    - scale : factor, ou width/height (ratio conservé si un seul est donné) ;
    - rotate : degrees (angle libre, sens horaire), autour du centre ou de (center_x, center_y) ;
    - flip : orientation horizontal|vertical ;
    - perspective : corners = [[x,y] haut-gauche, haut-droit, bas-gauche, bas-droit] en coordonnées image ;
    - shear : orientation + amount (pixels) ; offset : déplace de (dx, dy).
    clip : adjust (le calque grandit), clip (coupé à sa taille), crop (coupé au contenu)."""
    return _j(bridge.call("layer_transform", **_clean({"kind": kind, "image_id": image_id, "layer_id": layer_id,
                                                        "layer_name": layer_name, "factor": factor, "width": width,
                                                        "height": height, "degrees": degrees, "center_x": center_x,
                                                        "center_y": center_y, "orientation": orientation,
                                                        "corners": corners, "dx": dx, "dy": dy, "amount": amount,
                                                        "interpolation": interpolation, "clip": clip})))


@mcp.tool()
def gimp_layer_align(horizontal: Optional[str] = None, vertical: Optional[str] = None, dx: int = 0, dy: int = 0,
                     image_id: Optional[int] = None, layer_id: Optional[int] = None, layer_name: Optional[str] = None) -> str:
    """Aligne un calque sur l'image : horizontal left|center|right, vertical top|middle|bottom,
    plus un décalage (dx, dy)."""
    return _j(bridge.call("layer_align", **_clean({"horizontal": horizontal, "vertical": vertical, "dx": dx, "dy": dy,
                                                    "image_id": image_id, "layer_id": layer_id, "layer_name": layer_name})))


@mcp.tool()
def gimp_mask(action: str = "add", source: str = "white", invert: bool = False, edit: bool = False,
              image_id: Optional[int] = None, layer_id: Optional[int] = None, layer_name: Optional[str] = None) -> str:
    """Masque de calque. action :
    - add : crée le masque depuis source = white (tout visible), black (tout caché), alpha,
      transfer (alpha transféré), selection (la sélection courante), grayscale (copie du calque) ;
      invert=True l'inverse ; edit=True active l'édition du masque (les outils de peinture et
      de remplissage avec target="mask" agissent alors sur le masque) ;
    - apply (fusionne le masque dans le calque), delete, edit (active/désactive l'édition), show."""
    return _j(bridge.call("mask", **_clean({"action": action, "from": source, "invert": invert, "edit": edit,
                                             "image_id": image_id, "layer_id": layer_id, "layer_name": layer_name})))


# =============================================================================
# 3. Sélections et chemins
# =============================================================================

@mcp.tool()
def gimp_select(shape: str = "rectangle", mode: str = "replace", x: Optional[float] = None, y: Optional[float] = None,
                width: Optional[float] = None, height: Optional[float] = None, radius: Optional[float] = None,
                points: Optional[list] = None, color: Optional[str] = None, threshold: Optional[float] = None,
                feather: float = 0.0, antialias: bool = True, sample_merged: bool = False,
                path_id: Optional[int] = None, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                layer_name: Optional[str] = None) -> str:
    """Crée ou modifie la sélection. shape :
    - rectangle (x, y, width, height ; radius = coins arrondis) ; ellipse (idem) ;
    - polygon (points = [[x,y], ...]) : lasso polygonal ;
    - contiguous (x, y, threshold 0-1) : baguette magique sur le calque ;
    - color (color, threshold) : tous les pixels de cette couleur ;
    - path (path_id) ; layer_alpha (contour des pixels opaques du calque) ;
    - all, none, invert.
    mode : replace, add, subtract, intersect. feather = adoucissement en pixels."""
    return _j(bridge.call("select", **_clean({"shape": shape, "mode": mode, "x": x, "y": y, "width": width,
                                               "height": height, "radius": radius, "points": points, "color": color,
                                               "threshold": threshold, "feather": feather, "antialias": antialias,
                                               "sample_merged": sample_merged, "path_id": path_id,
                                               "image_id": image_id, "layer_id": layer_id, "layer_name": layer_name})))


@mcp.tool()
def gimp_selection_modify(action: str, amount: float = 1, image_id: Optional[int] = None) -> str:
    """Modifie la sélection courante : grow, shrink, border, feather (amount en pixels), sharpen, flood."""
    return _j(bridge.call("selection_modify", **_clean({"action": action, "amount": amount, "image_id": image_id})))


@mcp.tool()
def gimp_selection_channel(action: str, name: str = "Sélection", channel_id: Optional[int] = None,
                           mode: str = "replace", image_id: Optional[int] = None) -> str:
    """Sauvegarde (action=save) la sélection dans un canal nommé, ou la recharge (action=load,
    par channel_id ou name) avec le mode replace|add|subtract|intersect."""
    if action == "save":
        return _j(bridge.call("selection_save", **_clean({"name": name, "image_id": image_id})))
    return _j(bridge.call("selection_load", **_clean({"name": name, "channel_id": channel_id, "mode": mode,
                                                       "image_id": image_id})))


@mcp.tool()
def gimp_path_create(points: list, name: str = "Chemin", close: bool = False, image_id: Optional[int] = None) -> str:
    """Crée un chemin vectoriel. points : liste de [x, y] (segments droits) ou de
    [x, y, c1x, c1y, c2x, c2y] (courbe de Bézier vers (x, y) avec les deux poignées).
    Utile pour détourer précisément, puis gimp_select(shape="path") ou gimp_stroke."""
    return _j(bridge.call("path_create", **_clean({"points": points, "name": name, "close": close, "image_id": image_id})))


@mcp.tool()
def gimp_stroke(path_id: Optional[int] = None, selection: bool = False, color: str = "#000000", width: float = 3.0,
                opacity: float = 100.0, cap: str = "round", join: str = "round", image_id: Optional[int] = None,
                layer_id: Optional[int] = None, layer_name: Optional[str] = None, target: Optional[str] = None) -> str:
    """Trace le contour d'un chemin (path_id) ou de la sélection (selection=True) sur le calque,
    avec couleur, épaisseur et style d'extrémités. target="mask" pour tracer sur le masque."""
    return _j(bridge.call("path_stroke", **_clean({"path_id": path_id, "target_selection": selection, "color": color,
                                                    "width": width, "opacity": opacity, "cap": cap, "join": join,
                                                    "image_id": image_id, "layer_id": layer_id, "layer_name": layer_name,
                                                    "target": target})))


# =============================================================================
# 4. Peinture, remplissage, texte
# =============================================================================

@mcp.tool()
def gimp_fill(color: Optional[str] = None, pattern: Optional[str] = None, clear: bool = False,
              opacity: float = 100.0, mode: str = "normal", image_id: Optional[int] = None,
              layer_id: Optional[int] = None, layer_name: Optional[str] = None, target: Optional[str] = None) -> str:
    """Remplit la sélection (ou tout le calque s'il n'y a pas de sélection) avec une couleur,
    un motif (nom GIMP), ou efface en transparent (clear=True). target="mask" agit sur le masque
    (blanc = visible, noir = caché). Astuce dessin : gimp_select une forme puis gimp_fill."""
    return _j(bridge.call("fill", **_clean({"color": color, "pattern": pattern, "clear": clear, "opacity": opacity,
                                             "mode": mode, "image_id": image_id, "layer_id": layer_id,
                                             "layer_name": layer_name, "target": target})))


@mcp.tool()
def gimp_bucket_fill(x: float, y: float, color: str = "#000000", threshold: float = 0.15, sample_merged: bool = False,
                     opacity: float = 100.0, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                     layer_name: Optional[str] = None, target: Optional[str] = None) -> str:
    """Pot de peinture : remplit la zone de couleur similaire (threshold 0-1) autour de (x, y)."""
    return _j(bridge.call("bucket_fill", **_clean({"x": x, "y": y, "color": color, "threshold": threshold,
                                                    "sample_merged": sample_merged, "opacity": opacity,
                                                    "image_id": image_id, "layer_id": layer_id,
                                                    "layer_name": layer_name, "target": target})))


@mcp.tool()
def gimp_gradient(x1: float, y1: float, x2: float, y2: float, color_from: str = "#000000", color_to: str = "#ffffff",
                  shape: str = "linear", gradient: Optional[str] = None, repeat: str = "none", reverse: bool = False,
                  opacity: float = 100.0, mode: str = "normal", image_id: Optional[int] = None,
                  layer_id: Optional[int] = None, layer_name: Optional[str] = None, target: Optional[str] = None) -> str:
    """Dégradé de (x1, y1) vers (x2, y2), limité à la sélection s'il y en a une.
    shape : linear, bilinear, radial, square, conical-symmetric, conical-asymmetric,
    shapeburst-angular, shapeburst-spherical, shapeburst-dimpled, spiral-clockwise, spiral-anticlockwise.
    gradient : nom d'un dégradé GIMP (sinon color_from -> color_to). target="mask" pour un fondu de masque."""
    return _j(bridge.call("gradient", **_clean({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "from": color_from,
                                                 "to": color_to, "shape": shape, "gradient": gradient, "repeat": repeat,
                                                 "reverse": reverse, "opacity": opacity, "mode": mode,
                                                 "image_id": image_id, "layer_id": layer_id, "layer_name": layer_name,
                                                 "target": target})))


@mcp.tool()
def gimp_paint(points: list, tool: str = "paintbrush", color: Optional[str] = None, size: float = 20.0,
               sizes: Optional[list] = None, hardness: float = 0.5, opacity: float = 100.0,
               brush: Optional[str] = None, mode: str = "normal", pressure: float = 50.0, force: float = 0.5,
               exposure: float = 50.0, range: Optional[str] = None, image_id: Optional[int] = None,
               layer_id: Optional[int] = None, layer_name: Optional[str] = None, target: Optional[str] = None,
               source_layer_id: Optional[int] = None, source_x: Optional[float] = None,
               source_y: Optional[float] = None) -> str:
    """Coup de pinceau libre le long de points = [[x,y], ...] (un seul point = une touche).
    tool : paintbrush (bords doux selon hardness), pencil (bords nets), airbrush (pressure),
    eraser (efface vers transparent), smudge (étale, pressure), dodge (éclaircit), burn (assombrit)
    avec exposure 0-100 et range shadows|midtones|highlights, clone (copie depuis
    source_layer_id à partir de (source_x, source_y)).
    sizes : une taille de brosse par point (même longueur que points) pour un trait à épaisseur
    variable (fin-épais-fin, pointe qui se perd) en un seul appel et une seule annulation ;
    remplace size. Garde des points serrés (≤ 15 px) pour des transitions douces.
    brush : nom d'une brosse GIMP (gimp_resources kind="brushes") ; size en pixels ; hardness 0-1."""
    return _j(bridge.call("paint", **_clean({"points": points, "tool": tool, "color": color, "size": size,
                                              "sizes": sizes, "hardness": hardness, "opacity": opacity,
                                              "brush": brush, "mode": mode, "pressure": pressure, "force": force,
                                              "exposure": exposure, "range": range, "image_id": image_id,
                                              "layer_id": layer_id, "layer_name": layer_name, "target": target,
                                              "source_layer_id": source_layer_id, "source_x": source_x,
                                              "source_y": source_y})))


@mcp.tool()
def gimp_text(text: str, font: Optional[str] = None, size: float = 48, color: str = "#000000", x: int = 0, y: int = 0,
              justify: str = "left", width: Optional[int] = None, height: Optional[int] = None,
              letter_spacing: Optional[float] = None, line_spacing: Optional[float] = None, name: Optional[str] = None,
              opacity: Optional[float] = None, mode: Optional[str] = None, image_id: Optional[int] = None,
              parent_id: Optional[int] = None) -> str:
    """Ajoute un calque de texte (taille en pixels). font : nom exact d'une police installée
    (gimp_resources kind="fonts" filter="..."), sinon la police courante — vérifie `font_used`
    dans la réponse. width/height fixent une boîte avec retour à la ligne automatique.
    justify : left, right, center, fill."""
    return _j(bridge.call("text_add", **_clean({"text": text, "font": font, "size": size, "color": color, "x": x, "y": y,
                                                 "justify": justify, "width": width, "height": height,
                                                 "letter_spacing": letter_spacing, "line_spacing": line_spacing,
                                                 "name": name, "opacity": opacity, "mode": mode, "image_id": image_id,
                                                 "parent_id": parent_id})))


@mcp.tool()
def gimp_text_edit(layer_id: Optional[int] = None, layer_name: Optional[str] = None, text: Optional[str] = None,
                   font: Optional[str] = None, size: Optional[float] = None, color: Optional[str] = None,
                   justify: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None,
                   image_id: Optional[int] = None) -> str:
    """Modifie un calque de texte existant (texte, police, taille, couleur, justification, position)."""
    return _j(bridge.call("text_edit", **_clean({"layer_id": layer_id, "layer_name": layer_name, "text": text,
                                                  "font": font, "size": size, "color": color, "justify": justify,
                                                  "x": x, "y": y, "image_id": image_id})))


@mcp.tool()
def gimp_resources(kind: str = "fonts", filter: str = "", limit: int = 100) -> str:
    """Liste les ressources GIMP disponibles : fonts, brushes, patterns, gradients, palettes.
    filter = sous-chaîne (ex. "Sans", "Hardness")."""
    return _j(bridge.call("resources_list", kind=kind, filter=filter, limit=limit))


# =============================================================================
# 5. Couleur et filtres
# =============================================================================

@mcp.tool()
def gimp_adjust(kind: str, image_id: Optional[int] = None, layer_id: Optional[int] = None,
                layer_name: Optional[str] = None, target: Optional[str] = None, params: Optional[dict] = None) -> str:
    """Ajustement de couleur sur un calque (ou son masque avec target="mask"). kind et params :
    - brightness_contrast {brightness, contrast} en -100..100 ;
    - levels {channel, low_in, high_in, gamma, low_out, high_out} en 0..255, gamma ~0.1-10 ;
    - curves {channel, points:[[in,out],...]} en 0..255 (channel value|red|green|blue|alpha) ;
    - hue_saturation {hue -180..180, saturation -100..100, lightness -100..100, range all|red|yellow|green|cyan|blue|magenta} ;
    - color_balance {cyan_red, magenta_green, yellow_blue} -100..100, range shadows|midtones|highlights ;
    - colorize {hue 0-360, saturation 0-100, lightness -100..100} ;
    - desaturate {mode luminance|lightness|average|luma|value} ; invert ; posterize {levels} ;
    - threshold {low, high} 0..255 ; auto_levels ; shadows_highlights {shadows, highlights} -100..100."""
    return _j(bridge.call("adjust", **_clean({"kind": kind, "image_id": image_id, "layer_id": layer_id,
                                               "layer_name": layer_name, "target": target, **(params or {})})))


@mcp.tool()
def gimp_filter_search(query: str, limit: int = 60) -> str:
    """Recherche une opération GEGL (filtre) par mot-clé : blur, sharpen, noise, shadow, vignette,
    emboss, pixelize, cartoon, oilify, lens, distort, glow, bloom, edge, median, etc."""
    return _j(bridge.call("filter_search", query=query, limit=limit))


@mcp.tool()
def gimp_filter_describe(name: str) -> str:
    """Propriétés d'une opération GEGL (nom, type, bornes, défaut, choix) : à consulter avant gimp_filter_apply."""
    return _j(bridge.call("filter_describe", name=name))


@mcp.tool()
def gimp_filter_apply(name: str, params: Optional[dict] = None, opacity: float = 100.0, mode: Optional[str] = None,
                      non_destructive: bool = False, label: Optional[str] = None, image_id: Optional[int] = None,
                      layer_id: Optional[int] = None, layer_name: Optional[str] = None, target: Optional[str] = None) -> str:
    """Applique une opération GEGL sur un calque (limitée à la sélection s'il y en a une).
    Exemples : gegl:gaussian-blur {std-dev-x, std-dev-y} ; gegl:unsharp-mask {std-dev, scale} ;
    gegl:dropshadow {x, y, radius, color, opacity} ; gegl:vignette ; gegl:noise-rgb ; gegl:pixelize ;
    gegl:cartoon ; gegl:oilify ; gegl:bloom ; gegl:color-temperature ; gegl:exposure ; gegl:saturation.
    non_destructive=True l'attache comme effet modifiable (GIMP 3) au lieu de le fusionner."""
    return _j(bridge.call("filter_apply", **_clean({"name": name, "params": params, "opacity": opacity, "mode": mode,
                                                     "non_destructive": non_destructive, "label": label,
                                                     "image_id": image_id, "layer_id": layer_id,
                                                     "layer_name": layer_name, "target": target})))


@mcp.tool()
def gimp_layer_effects(action: str = "list", filter_id: Optional[int] = None, image_id: Optional[int] = None,
                       layer_id: Optional[int] = None, layer_name: Optional[str] = None) -> str:
    """Effets non destructifs d'un calque : list, remove (filter_id), merge_all (fusionne tous les effets)."""
    target = {"image_id": image_id, "layer_id": layer_id, "layer_name": layer_name}
    if action == "list":
        return _j(bridge.call("filter_list_on_layer", **_clean(target)))
    if action == "remove":
        return _j(bridge.call("filter_remove", **_clean({**target, "filter_id": filter_id})))
    if action == "merge_all":
        return _j(bridge.call("filter_remove", **_clean({**target, "merge_all": True})))
    return "action inconnue : list, remove, merge_all"


# =============================================================================
# 6. PDB et Python brut
# =============================================================================

@mcp.tool()
def gimp_pdb_search(query: str, limit: int = 60) -> str:
    """Recherche des procédures du PDB de GIMP (regex sur le nom et la description),
    ex. "gimp-image-.*", "text", "selection". Pour tout ce que les outils typés ne couvrent pas."""
    return _j(bridge.call("pdb_search", query=query, limit=limit))


@mcp.tool()
def gimp_pdb_describe(name: str) -> str:
    """Arguments et valeurs de retour d'une procédure PDB (avec types, défauts, choix d'énumération)."""
    return _j(bridge.call("pdb_describe", name=name))


@mcp.tool()
def gimp_pdb_call(name: str, args: Optional[dict] = None) -> str:
    """Appelle une procédure PDB. args : dictionnaire nom -> valeur ; images et calques par id,
    énumérations par nick ("rgb-image"), couleurs en hex, fichiers par chemin. run-mode est
    mis à non-interactive automatiquement."""
    return _j(bridge.call("pdb_call", name=name, args=args or {}))


@mcp.tool()
def gimp_run_python(code: str, mode: str = "exec") -> str:
    """Exécute du Python directement dans le processus GIMP (API GObject Introspection de GIMP 3.2).
    Espace de noms persistant avec Gimp, Gegl, Gio, GLib, GObject, pdb, image(), layer(image, ...),
    color("#hex"). mode="exec" : affecte `result` pour renvoyer une valeur (stdout capturé) ;
    mode="eval" : renvoie la valeur de l'expression. Dernier recours quand aucun outil ne convient."""
    return _j(bridge.call("run_python", code=code, mode=mode))


def main():
    """Point d'entrée : serveur MCP en stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
