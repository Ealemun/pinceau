"""
Pinceau — opérations exécutées dans le processus GIMP.

Chaque fonction `op_xxx(p)` reçoit le dictionnaire `params` de la requête et
renvoie une valeur sérialisable en JSON. Le dispatcher `dispatch(cmd, params)`
fait le lien entre le nom de commande et la fonction.

Conventions :
  - `image_id` optionnel partout : sinon on prend l'image courante (STATE)
    ou, à défaut, la première image ouverte ;
  - `layer_id` ou `layer_name` optionnels : sinon le calque sélectionné dans
    GIMP, ou le calque du dessus ;
  - chaque opération qui modifie l'image est enveloppée dans un groupe
    d'annulation : un appel = un Ctrl+Z pour l'utilisateur ;
  - les couleurs s'écrivent "#rrggbb", "#rrggbbaa" ou [r, g, b(, a)] 0..255.

Compatibilité : GIMP 3.2 (quelques repliques pour 3.0 via getattr).
"""

import base64
import contextlib
import io
import os
import tempfile

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gegl, Gimp, Gio, GLib, GObject  # noqa: E402

VERSION = "0.1.0"
SERVER = None  # renseigné par pinceau.py (pour un arrêt depuis run_python)

# État partagé entre les appels : image "courante" et espace de noms Python
STATE = {"image_id": None}
PY_NAMESPACE = None  # créé à la première exécution de run_python

# Classes dont le nom a changé entre GIMP 3.0 et 3.2
PathClass = getattr(Gimp, "Path", None) or getattr(Gimp, "Vectors", None)


# =============================================================================
# Utilitaires
# =============================================================================

def _err(message):
    """Lève une erreur lisible côté client."""
    raise ValueError(message)


def _check(value, action):
    """Vérifie le retour d'un appel GIMP et lève une erreur lisible en cas d'échec.

    Le plugin gère lui-même les erreurs PDB (pas de fenêtre d'erreur dans GIMP) :
    un appel qui échoue renvoie False ou None, et GIMP garde le message dans
    pdb.get_last_error(). Cette fonction le remonte au client MCP.
    """
    if value is None or value is False:
        _err(f"{action} a échoué : {Gimp.get_pdb().get_last_error()}")
    return value


def _all_images():
    """Liste des images ouvertes, quelle que soit la version de l'API."""
    for name in ("get_images", "list_images"):
        fn = getattr(Gimp, name, None)
        if fn is not None:
            return list(fn())
    return []


def _image(p):
    """Résout l'image visée par la requête (voir conventions en tête)."""
    image_id = p.get("image_id", STATE.get("image_id"))
    if image_id is not None:
        image = Gimp.Image.get_by_id(int(image_id))
        if image is not None and image.is_valid():
            return image
        if p.get("image_id") is not None:
            _err(f"image {image_id} introuvable (fermée ?)")
    images = _all_images()
    if not images:
        _err("aucune image ouverte : utilise image_new ou image_open")
    STATE["image_id"] = images[0].get_id()
    return images[0]


def _iter_layers(layers):
    """Parcourt récursivement les calques et les groupes."""
    for layer in layers:
        yield layer
        if layer.is_group():
            yield from _iter_layers(layer.get_children())


def _find_layer_by_name(image, name):
    for layer in _iter_layers(image.get_layers()):
        if layer.get_name() == name:
            return layer
    return None


def _layer(image, p, key_id="layer_id", key_name="layer_name", required=True):
    """Résout le calque visé : par id, par nom, sinon le calque sélectionné."""
    if p.get(key_id) is not None:
        layer = Gimp.Layer.get_by_id(int(p[key_id]))
        if layer is None or not layer.is_valid():
            _err(f"calque {p[key_id]} introuvable")
        return layer
    if p.get(key_name):
        layer = _find_layer_by_name(image, p[key_name])
        if layer is None:
            _err(f"calque « {p[key_name]} » introuvable")
        return layer
    selected = image.get_selected_layers()
    if selected:
        return selected[0]
    layers = image.get_layers()
    if layers:
        return layers[0]
    if required:
        _err("l'image n'a aucun calque")
    return None


def _drawable(image, p):
    """Le drawable à modifier : le masque si on l'édite, sinon le calque."""
    layer = _layer(image, p)
    if p.get("target") == "mask":
        mask = layer.get_mask()
        if mask is None:
            _err("ce calque n'a pas de masque")
        return mask
    return layer


def _color(value, default="#000000"):
    """Convertit "#rrggbb(aa)" ou [r,g,b(,a)] 0..255 en Gegl.Color."""
    if value is None:
        value = default
    color = Gegl.Color.new("black")
    if isinstance(value, str):
        v = value.strip().lstrip("#")
        if len(v) == 6:
            v += "ff"
        if len(v) != 8:
            _err(f"couleur invalide : {value}")
        r, g, b, a = (int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4, 6))
    else:
        parts = list(value) + [255] * (4 - len(value))
        r, g, b, a = (float(x) / 255.0 for x in parts[:4])
    color.set_rgba(r, g, b, a)
    return color


def _color_to_hex(color):
    """Gegl.Color -> "#rrggbbaa"."""
    r, g, b, a = color.get_rgba()
    return "#%02x%02x%02x%02x" % tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in (r, g, b, a))


def _enum(enum_class, value, default=None):
    """"overlay" / "OVERLAY" / "hard-light" -> membre de l'énumération GIMP."""
    if value is None:
        return default
    if isinstance(value, int):
        return enum_class(value)
    name = str(value).upper().replace("-", "_").replace(" ", "_")
    try:
        return getattr(enum_class, name)
    except AttributeError:
        valid = [n for n in dir(enum_class) if n.isupper()]
        _err(f"valeur « {value} » inconnue ; valeurs possibles : {', '.join(valid)}")


@contextlib.contextmanager
def _undo(image):
    """Regroupe les modifications d'un appel en une seule étape d'annulation."""
    image.undo_group_start()
    try:
        yield
    finally:
        image.undo_group_end()
        Gimp.displays_flush()


def _describe_layer(layer, deep=True):
    """Description JSON d'un calque (et de ses enfants pour un groupe)."""
    ok, ox, oy = layer.get_offsets()
    info = {
        "id": layer.get_id(),
        "name": layer.get_name(),
        "kind": "group" if layer.is_group() else ("text" if layer.is_text_layer() else "layer"),
        "width": layer.get_width(),
        "height": layer.get_height(),
        "x": ox,
        "y": oy,
        "opacity": round(layer.get_opacity(), 2),
        "mode": layer.get_mode().value_nick,
        "visible": layer.get_visible(),
        "has_alpha": layer.has_alpha(),
        "has_mask": layer.get_mask() is not None,
        "lock_alpha": layer.get_lock_alpha(),
    }
    if layer.is_text_layer():
        try:
            info["text"] = layer.get_text()
        except Exception:  # noqa: BLE001
            pass
    if deep and layer.is_group():
        info["children"] = [_describe_layer(c) for c in layer.get_children()]
    return info


def _describe_image(image):
    """Description JSON d'une image."""
    _ok, ok, x1, y1, x2, y2 = Gimp.Selection.bounds(image)  # (succès, non vide, x1, y1, x2, y2)
    f = image.get_file()
    return {
        "id": image.get_id(),
        "name": image.get_name(),
        "file": f.get_path() if f is not None else None,
        "width": image.get_width(),
        "height": image.get_height(),
        "base_type": image.get_base_type().value_nick,
        "precision": image.get_precision().value_nick,
        "selection": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1} if ok else None,
        "selected_layer_ids": [l.get_id() for l in image.get_selected_layers()],
        "layers": [_describe_layer(l) for l in image.get_layers()],
        "paths": [{"id": v.get_id(), "name": v.get_name()} for v in _image_paths(image)],
    }


def _image_paths(image):
    """Liste des chemins (vecteurs) d'une image, 3.0 ou 3.2."""
    for name in ("get_paths", "get_vectors", "list_paths", "list_vectors"):
        fn = getattr(image, name, None)
        if fn is not None:
            return list(fn())
    return []


def _set_current(image):
    STATE["image_id"] = image.get_id()


def _export(image, path):
    """Sauvegarde/export via les paramètres par défaut du format (extension)."""
    gfile = Gio.File.new_for_path(path)
    ok = Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, gfile, None)
    if not ok:
        _err(f"échec de l'export vers {path}")


def _png_bytes_of_image(image, max_size=None, region=None):
    """Aplatit une COPIE de l'image et renvoie les octets PNG (l'original est intact)."""
    dup = image.duplicate()
    try:
        if region:
            x, y, w, h = (int(region[k]) for k in ("x", "y", "width", "height"))
            _check(dup.crop(w, h, x, y), "crop")
        try:
            dup.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
        except Exception:  # noqa: BLE001 — aucun calque visible : on exporte tel quel
            pass
        layers = dup.get_layers()
        if layers:
            layers[0].resize_to_image_size()
        if max_size:
            w, h = dup.get_width(), dup.get_height()
            scale = min(1.0, float(max_size) / max(w, h))
            if scale < 1.0:
                _check(dup.scale(max(1, int(w * scale)), max(1, int(h * scale))), "scale")
        return _png_bytes_via_tempfile(dup)
    finally:
        dup.delete()


def _png_bytes_via_tempfile(image):
    """Exporte une image en PNG dans un fichier temporaire et lit les octets."""
    fd, path = tempfile.mkstemp(prefix="pinceau-", suffix=".png")
    os.close(fd)
    try:
        _export(image, path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _pspec_info(pspec):
    """Décrit un GObject.ParamSpec (argument PDB ou propriété GEGL) en JSON."""
    info = {"name": pspec.name, "type": pspec.value_type.name, "doc": pspec.blurb or ""}
    for attr in ("minimum", "maximum"):
        if hasattr(pspec, attr):
            try:
                info[attr] = getattr(pspec, attr)
            except Exception:  # noqa: BLE001
                pass
    try:
        default = pspec.default_value
        if isinstance(default, (int, float, str, bool)) or default is None:
            info["default"] = default
        elif hasattr(default, "value_nick"):
            info["default"] = default.value_nick
    except Exception:  # noqa: BLE001
        pass
    if GObject.type_is_a(pspec.value_type, GObject.TYPE_ENUM):
        try:
            info["choices"] = [v.value_nick for v in pspec.value_type.pytype.__enum_values__.values()]
        except Exception:  # noqa: BLE001
            try:
                enum_class = GObject.type_class_ref(pspec.value_type)
                info["choices"] = [v.value_nick for v in enum_class.__enum_values__.values()]
            except Exception:  # noqa: BLE001
                pass
    return info


def _json_to_gvalue(value, pspec):
    """Convertit une valeur JSON vers le type attendu par un ParamSpec."""
    vtype = pspec.value_type
    if GObject.type_is_a(vtype, Gimp.Image.__gtype__):
        return Gimp.Image.get_by_id(int(value))
    if GObject.type_is_a(vtype, Gimp.Item.__gtype__):
        return Gimp.Item.get_by_id(int(value))
    if GObject.type_is_a(vtype, Gegl.Color.__gtype__):
        return _color(value)
    if GObject.type_is_a(vtype, Gio.File.__gtype__):
        return Gio.File.new_for_path(str(value))
    if GObject.type_is_a(vtype, Gimp.Resource.__gtype__):
        for cls in (Gimp.Brush, Gimp.Font, Gimp.Pattern, Gimp.Gradient, Gimp.Palette):
            if GObject.type_is_a(vtype, cls.__gtype__):
                res = cls.get_by_name(str(value))
                if res is None:
                    _err(f"ressource « {value} » introuvable ({cls.__name__})")
                return res
    if GObject.type_is_a(vtype, Gimp.Unit.__gtype__):
        return Gimp.Unit.pixel() if str(value) in ("px", "pixel") else Gimp.Unit.get_by_id(int(value))
    if GObject.type_is_a(vtype, GObject.TYPE_ENUM) and isinstance(value, str):
        # PyGObject accepte le "nick" d'une énumération sous forme de chaîne
        return value
    if GObject.type_is_a(vtype, GObject.TYPE_DOUBLE) or GObject.type_is_a(vtype, GObject.TYPE_FLOAT):
        return float(value)
    if GObject.type_is_a(vtype, GObject.TYPE_BOOLEAN):
        return bool(value)
    if GObject.type_is_a(vtype, GObject.TYPE_INT) or GObject.type_is_a(vtype, GObject.TYPE_UINT):
        return int(value)
    if vtype.name.endswith("ObjectArray") or vtype.name == "GimpCoreObjectArray":
        return [Gimp.Item.get_by_id(int(v)) for v in value]
    return value


def _gvalue_to_json(value):
    """Convertit une valeur renvoyée par GIMP en JSON (objets -> id)."""
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, Gimp.Image):
        return {"image_id": value.get_id()}
    if isinstance(value, Gimp.Item):
        return {"item_id": value.get_id(), "name": value.get_name(), "type": type(value).__name__}
    if isinstance(value, Gegl.Color):
        return _color_to_hex(value)
    if isinstance(value, Gio.File):
        return value.get_path()
    if hasattr(value, "value_nick"):
        return value.value_nick
    if hasattr(value, "get_name"):
        try:
            return value.get_name()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple)):
        return [_gvalue_to_json(v) for v in value]
    return str(value)


# =============================================================================
# Session / vision
# =============================================================================

def op_status(p):
    """Version, images ouvertes, image courante, contexte (couleurs, pinceau)."""
    images = _all_images()
    return {
        "pinceau": VERSION,
        "gimp": Gimp.version(),
        "current_image_id": STATE.get("image_id"),
        "images": [{"id": i.get_id(), "name": i.get_name(), "width": i.get_width(),
                    "height": i.get_height()} for i in images],
        "context": {
            "foreground": _color_to_hex(Gimp.context_get_foreground()),
            "background": _color_to_hex(Gimp.context_get_background()),
            "brush": Gimp.context_get_brush().get_name(),
            "brush_size": Gimp.context_get_brush_size(),
            "opacity": Gimp.context_get_opacity(),
            "font": Gimp.context_get_font().get_name(),
        },
    }


def op_image_info(p):
    """Description complète d'une image (calques, sélection, chemins)."""
    return _describe_image(_image(p))


def op_set_current_image(p):
    image = _image({"image_id": p["image_id"]})
    _set_current(image)
    return _describe_image(image)


def op_snapshot(p):
    """Aperçu PNG (base64) de l'image composite, d'un calque ou d'une région."""
    image = _image(p)
    max_size = p.get("max_size", 1024)
    if p.get("layer_id") is not None or p.get("layer_name"):
        # Aperçu d'un seul calque : on le copie dans une image temporaire
        layer = _layer(image, p)
        tmp = Gimp.Image.new(layer.get_width(), layer.get_height(), Gimp.ImageBaseType.RGB)
        try:
            copy = Gimp.Layer.new_from_drawable(layer, tmp)
            _check(tmp.insert_layer(copy, None, 0), "insert_layer")
            _check(copy.set_offsets(0, 0), "set_offsets")
            copy.set_visible(True)
            copy.set_opacity(100.0)
            data = _png_bytes_of_image(tmp, max_size=max_size)
        finally:
            tmp.delete()
    else:
        data = _png_bytes_of_image(image, max_size=max_size, region=p.get("region"))
    return {"png_base64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}


def op_pick_color(p):
    """Couleur à (x, y), sur le calque ou l'image fusionnée."""
    image = _image(p)
    layer = _layer(image, p)
    ok, color = image.pick_color([layer], float(p["x"]), float(p["y"]),
                                 bool(p.get("sample_merged", True)),
                                 bool(p.get("average", False)), float(p.get("radius", 0.0)))
    if not ok:
        _err("point hors de l'image")
    r, g, b, a = color.get_rgba()
    return {"hex": _color_to_hex(color), "rgba": [round(r * 255), round(g * 255), round(b * 255), round(a * 255)]}


def op_histogram(p):
    """Statistiques d'histogramme d'un calque (canal value par défaut)."""
    image = _image(p)
    d = _drawable(image, p)
    channel = _enum(Gimp.HistogramChannel, p.get("channel"), Gimp.HistogramChannel.VALUE)
    ok, mean, std_dev, median, pixels, count, percentile = d.histogram(
        channel, float(p.get("start", 0.0)), float(p.get("end", 1.0)))
    return {"mean": mean, "std_dev": std_dev, "median": median, "pixels": pixels,
            "count": count, "percentile": percentile}


# =============================================================================
# Fichiers / images
# =============================================================================

def op_image_new(p):
    """Nouvelle image RVB avec un calque de fond (transparent ou coloré)."""
    w, h = int(p.get("width", 1920)), int(p.get("height", 1080))
    image = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)
    layer = Gimp.Layer.new(image, p.get("layer_name", "Fond"), w, h,
                           Gimp.ImageType.RGBA_IMAGE, 100.0, Gimp.LayerMode.NORMAL)
    _check(image.insert_layer(layer, None, 0), "insert_layer")
    fill = p.get("fill")  # None = transparent
    if fill:
        Gimp.context_push()
        try:
            Gimp.context_set_foreground(_color(fill))
            _check(layer.edit_fill(Gimp.FillType.FOREGROUND), "edit_fill")
        finally:
            Gimp.context_pop()
    else:
        _check(layer.edit_clear(), "edit_clear")
    if p.get("display", True):
        Gimp.Display.new(image)
    _set_current(image)
    Gimp.displays_flush()
    return _describe_image(image)


def op_image_open(p):
    """Ouvre un fichier image (tout format supporté par GIMP)."""
    path = os.path.expanduser(p["path"])
    if not os.path.isfile(path):
        _err(f"fichier introuvable : {path}")
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
    if image is None:
        _err(f"GIMP n'a pas pu ouvrir {path}")
    if p.get("display", True):
        Gimp.Display.new(image)
    _set_current(image)
    Gimp.displays_flush()
    return _describe_image(image)


def op_image_place(p):
    """Photomontage : insère un fichier image comme nouveau calque."""
    image = _image(p)
    path = os.path.expanduser(p["path"])
    if not os.path.isfile(path):
        _err(f"fichier introuvable : {path}")
    with _undo(image):
        layer = Gimp.file_load_layer(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(path))
        if layer is None:
            _err(f"GIMP n'a pas pu charger {path}")
        parent = _layer(image, p, "parent_id", "parent_name", required=False) if (p.get("parent_id") or p.get("parent_name")) else None
        _check(image.insert_layer(layer, parent, int(p.get("position", 0))), "insert_layer")
        if p.get("name"):
            layer.set_name(p["name"])
        if not layer.has_alpha():
            layer.add_alpha()
        # Redimensionnement optionnel (largeur et/ou hauteur cibles, ou facteur)
        scale = p.get("scale")
        tw, th = p.get("width"), p.get("height")
        if scale or tw or th:
            lw, lh = layer.get_width(), layer.get_height()
            if scale:
                tw, th = lw * float(scale), lh * float(scale)
            elif tw and not th:
                th = lh * float(tw) / lw
            elif th and not tw:
                tw = lw * float(th) / lh
            _check(layer.scale(max(1, int(tw)), max(1, int(th)), False), "scale")
        _check(layer.set_offsets(int(p.get("x", 0)), int(p.get("y", 0))), "set_offsets")
        if p.get("opacity") is not None:
            layer.set_opacity(float(p["opacity"]))
        if p.get("mode"):
            layer.set_mode(_enum(Gimp.LayerMode, p["mode"]))
        image.set_selected_layers([layer])
    return _describe_layer(layer)


def op_image_save_xcf(p):
    """Sauvegarde le projet complet (calques, masques...) en XCF."""
    image = _image(p)
    path = os.path.expanduser(p["path"])
    if not path.lower().endswith(".xcf"):
        path += ".xcf"
    _export(image, path)
    image.set_file(Gio.File.new_for_path(path))
    image.clean_all()
    return {"path": path}


def op_image_export(p):
    """Exporte l'image aplatie (PNG, JPEG, WebP, TIFF, BMP... selon l'extension)."""
    image = _image(p)
    path = os.path.expanduser(p["path"])
    max_size = p.get("max_size")
    if max_size:
        # Export réduit : on travaille sur une copie aplatie et redimensionnée
        dup = image.duplicate()
        try:
            try:
                dup.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
            except Exception:  # noqa: BLE001
                pass
            w, h = dup.get_width(), dup.get_height()
            scale = min(1.0, float(max_size) / max(w, h))
            if scale < 1.0:
                _check(dup.scale(max(1, int(w * scale)), max(1, int(h * scale))), "scale")
            _export(dup, path)
        finally:
            dup.delete()
        return {"path": path, "bytes": os.path.getsize(path)}
    # Export direct : GIMP fusionne lui-même les calques pour les formats plats
    _export(image, path)
    return {"path": path, "bytes": os.path.getsize(path)}


def op_image_close(p):
    """Ferme une image (sans sauvegarder). Si une fenêtre l'affiche, GIMP peut refuser."""
    image = _image(p)
    image_id = image.get_id()
    if not image.delete():  # renvoie False si une fenêtre affiche encore l'image
        _err("GIMP refuse de fermer l'image (fenêtre ouverte ?). Ferme-la depuis GIMP.")
    if STATE.get("image_id") == image_id:
        STATE["image_id"] = None
    return {"closed": image_id}


def op_image_resize_canvas(p):
    """Change la taille du canevas sans redimensionner les pixels."""
    image = _image(p)
    with _undo(image):
        _check(image.resize(int(p["width"]), int(p["height"]), int(p.get("offset_x", 0)), int(p.get("offset_y", 0))), "resize")
        if p.get("resize_layers", False):
            for layer in _iter_layers(image.get_layers()):
                if not layer.is_group():
                    layer.resize_to_image_size()
    return _describe_image(image)


def op_image_scale(p):
    image = _image(p)
    with _undo(image):
        Gimp.context_push()
        Gimp.context_set_interpolation(_enum(Gimp.InterpolationType, p.get("interpolation"), Gimp.InterpolationType.LOHALO))
        try:
            _check(image.scale(int(p["width"]), int(p["height"])), "scale")
        finally:
            Gimp.context_pop()
    return _describe_image(image)


def op_image_crop(p):
    """Recadre à la sélection (défaut) ou à un rectangle explicite."""
    image = _image(p)
    with _undo(image):
        if p.get("width") and p.get("height"):
            _check(image.crop(int(p["width"]), int(p["height"]), int(p.get("x", 0)), int(p.get("y", 0))), "crop")
        else:
            _ok, ok, x1, y1, x2, y2 = Gimp.Selection.bounds(image)  # (succès, non vide, x1, y1, x2, y2)
            if not ok:
                _err("aucune sélection et aucun rectangle fourni")
            _check(image.crop(x2 - x1, y2 - y1, x1, y1), "crop")
    return _describe_image(image)


def op_image_rotate(p):
    """Rotation de l'image entière : 90, 180 ou 270 degrés."""
    image = _image(p)
    deg = int(p.get("degrees", 90)) % 360
    rot = {90: Gimp.RotationType.DEGREES90, 180: Gimp.RotationType.DEGREES180, 270: Gimp.RotationType.DEGREES270}
    if deg not in rot:
        _err("degrees doit valoir 90, 180 ou 270 (sinon utilise layer_transform rotate)")
    with _undo(image):
        _check(image.rotate(rot[deg]), "rotate")
    return _describe_image(image)


def op_image_flip(p):
    image = _image(p)
    with _undo(image):
        _check(image.flip(_enum(Gimp.OrientationType, p.get("orientation", "horizontal"))), "flip")
    return _describe_image(image)


def op_image_flatten(p):
    image = _image(p)
    with _undo(image):
        _check(image.flatten(), "flatten")
    return _describe_image(image)


def op_image_merge_visible(p):
    image = _image(p)
    with _undo(image):
        layer = image.merge_visible_layers(_enum(Gimp.MergeType, p.get("merge_type"), Gimp.MergeType.EXPAND_AS_NECESSARY))
    return _describe_layer(layer)


# =============================================================================
# Calques
# =============================================================================

def op_layer_list(p):
    image = _image(p)
    return [_describe_layer(l) for l in image.get_layers()]


def _parent_and_position(image, p):
    """Parent (groupe) et position d'insertion demandés."""
    parent = None
    if p.get("parent_id") is not None or p.get("parent_name"):
        parent = _layer(image, p, "parent_id", "parent_name")
        if not parent.is_group():
            _err("parent_id / parent_name doit désigner un groupe")
    return parent, int(p.get("position", 0))


def op_layer_create(p):
    """Nouveau calque transparent (ou rempli) ; position 0 = au-dessus."""
    image = _image(p)
    w = int(p.get("width", image.get_width()))
    h = int(p.get("height", image.get_height()))
    with _undo(image):
        layer = Gimp.Layer.new(image, p.get("name", "Calque"), w, h, Gimp.ImageType.RGBA_IMAGE,
                               float(p.get("opacity", 100.0)), _enum(Gimp.LayerMode, p.get("mode"), Gimp.LayerMode.NORMAL))
        parent, position = _parent_and_position(image, p)
        _check(image.insert_layer(layer, parent, position), "insert_layer")
        if p.get("fill"):
            Gimp.context_push()
            try:
                Gimp.context_set_foreground(_color(p["fill"]))
                _check(layer.edit_fill(Gimp.FillType.FOREGROUND), "edit_fill")
            finally:
                Gimp.context_pop()
        else:
            _check(layer.edit_clear(), "edit_clear")
        _check(layer.set_offsets(int(p.get("x", 0)), int(p.get("y", 0))), "set_offsets")
        image.set_selected_layers([layer])
    return _describe_layer(layer)


def op_group_create(p):
    """Nouveau groupe de calques (vide, ou contenant les calques listés)."""
    image = _image(p)
    with _undo(image):
        group = Gimp.GroupLayer.new(image, p.get("name", "Groupe"))
        parent, position = _parent_and_position(image, p)
        _check(image.insert_layer(group, parent, position), "insert_layer")
        for i, lid in enumerate(p.get("layer_ids") or []):
            child = Gimp.Layer.get_by_id(int(lid))
            if child is not None:
                _check(image.reorder_item(child, group, i), "reorder_item")
        image.set_selected_layers([group])
    return _describe_layer(group)


def op_layer_duplicate(p):
    image = _image(p)
    layer = _layer(image, p)
    with _undo(image):
        copy = layer.copy()
        _check(image.insert_layer(copy, layer.get_parent(), image.get_item_position(layer)), "insert_layer")
        if p.get("name"):
            copy.set_name(p["name"])
        image.set_selected_layers([copy])
    return _describe_layer(copy)


def op_layer_delete(p):
    image = _image(p)
    layer = _layer(image, p)
    name = layer.get_name()
    with _undo(image):
        _check(image.remove_layer(layer), "remove_layer")
    return {"deleted": name}


def op_layer_set(p):
    """Propriétés d'un calque : nom, opacité, mode, visibilité, verrous, position."""
    image = _image(p)
    layer = _layer(image, p)
    with _undo(image):
        if p.get("name") is not None:
            layer.set_name(p["name"])
        if p.get("opacity") is not None:
            layer.set_opacity(float(p["opacity"]))
        if p.get("mode") is not None:
            layer.set_mode(_enum(Gimp.LayerMode, p["mode"]))
        if p.get("visible") is not None:
            layer.set_visible(bool(p["visible"]))
        if p.get("lock_alpha") is not None:
            layer.set_lock_alpha(bool(p["lock_alpha"]))
        if p.get("lock_content") is not None:
            layer.set_lock_content(bool(p["lock_content"]))
        if p.get("x") is not None or p.get("y") is not None:
            ok, ox, oy = layer.get_offsets()
            _check(layer.set_offsets(int(p.get("x", ox)), int(p.get("y", oy))), "set_offsets")
        if p.get("add_alpha") and not layer.has_alpha():
            layer.add_alpha()
        if p.get("select", True):
            image.set_selected_layers([layer])
    return _describe_layer(layer)


def op_layer_move(p):
    """Déplace un calque dans la pile (position, et/ou dans un groupe)."""
    image = _image(p)
    layer = _layer(image, p)
    with _undo(image):
        parent = layer.get_parent()
        if "parent_id" in p or "parent_name" in p:
            parent = None
            if p.get("parent_id") is not None or p.get("parent_name"):
                parent = _layer(image, p, "parent_id", "parent_name")
        _check(image.reorder_item(layer, parent, int(p.get("position", 0))), "reorder_item")
    return _describe_layer(layer)


def op_layer_merge_down(p):
    """Fusionne un calque avec celui du dessous (les deux doivent être visibles)."""
    image = _image(p)
    layer = _layer(image, p)
    # GIMP refuse de fusionner un calque invisible, ou sur un calque invisible :
    # on le signale clairement plutôt que de laisser échouer la procédure.
    parent = layer.get_parent()
    siblings = parent.get_children() if parent is not None else image.get_layers()
    index = image.get_item_position(layer)
    if index + 1 >= len(siblings):
        _err(f"« {layer.get_name()} » est le calque le plus bas de sa pile : rien à fusionner dessous")
    below = siblings[index + 1]
    hidden = [l.get_name() for l in (layer, below) if not l.get_visible()]
    if hidden:
        _err("fusion impossible, calque(s) invisible(s) : " + ", ".join(f"« {n} »" for n in hidden)
             + ". Rends-les visibles avec gimp_layer_set visible=true, puis recommence.")
    with _undo(image):
        merged = _check(image.merge_down(layer, _enum(Gimp.MergeType, p.get("merge_type"),
                                                     Gimp.MergeType.EXPAND_AS_NECESSARY)), "merge_down")
    return _describe_layer(merged)


def op_layer_resize_to_image(p):
    image = _image(p)
    layer = _layer(image, p)
    with _undo(image):
        layer.resize_to_image_size()
    return _describe_layer(layer)


def op_layer_transform(p):
    """Transformations géométriques d'un calque : scale, rotate, flip, perspective, offset."""
    image = _image(p)
    layer = _layer(image, p)
    kind = p.get("kind")
    with _undo(image):
        Gimp.context_push()
        try:
            Gimp.context_set_interpolation(_enum(Gimp.InterpolationType, p.get("interpolation"), Gimp.InterpolationType.CUBIC))
            Gimp.context_set_transform_resize(_enum(Gimp.TransformResize, p.get("clip"), Gimp.TransformResize.ADJUST))
            ok, ox, oy = layer.get_offsets()
            lw, lh = layer.get_width(), layer.get_height()
            if kind == "scale":
                if p.get("factor"):
                    tw, th = lw * float(p["factor"]), lh * float(p["factor"])
                else:
                    tw = float(p.get("width", lw))
                    th = float(p.get("height", lh * tw / lw if p.get("width") and not p.get("height") else lh))
                    if p.get("height") and not p.get("width"):
                        tw = lw * th / lh
                _check(layer.scale(max(1, int(tw)), max(1, int(th)), bool(p.get("local_origin", False))), "scale")
            elif kind == "rotate":
                import math
                angle = math.radians(float(p.get("degrees", 0)))
                auto = p.get("center_x") is None
                _check(layer.transform_rotate(angle, auto, float(p.get("center_x", 0)), float(p.get("center_y", 0))), "transform_rotate")
            elif kind == "flip":
                _check(layer.transform_flip_simple(_enum(Gimp.OrientationType, p.get("orientation", "horizontal")), True, 0.0), "transform_flip_simple")
            elif kind == "perspective":
                c = p["corners"]  # [[x0,y0],[x1,y1],[x2,y2],[x3,y3]] : haut-gauche, haut-droit, bas-gauche, bas-droit
                _check(layer.transform_perspective(*(float(v) for pair in c for v in pair)), "transform_perspective")
            elif kind == "offset":
                _check(layer.set_offsets(ox + int(p.get("dx", 0)), oy + int(p.get("dy", 0))), "set_offsets")
            elif kind == "shear":
                _check(layer.transform_shear(_enum(Gimp.OrientationType, p.get("orientation", "horizontal")), float(p.get("amount", 0))), "transform_shear")
            else:
                _err("kind doit être scale, rotate, flip, perspective, shear ou offset")
        finally:
            Gimp.context_pop()
    return _describe_layer(layer)


def op_layer_align(p):
    """Aligne un calque sur l'image : left/center/right × top/middle/bottom."""
    image = _image(p)
    layer = _layer(image, p)
    iw, ih = image.get_width(), image.get_height()
    lw, lh = layer.get_width(), layer.get_height()
    ok, ox, oy = layer.get_offsets()
    hx = {"left": 0, "center": (iw - lw) // 2, "right": iw - lw}.get(p.get("horizontal"), ox)
    vy = {"top": 0, "middle": (ih - lh) // 2, "bottom": ih - lh}.get(p.get("vertical"), oy)
    with _undo(image):
        _check(layer.set_offsets(hx + int(p.get("dx", 0)), vy + int(p.get("dy", 0))), "set_offsets")
    return _describe_layer(layer)


# =============================================================================
# Masques
# =============================================================================

def op_mask(p):
    """Masque de calque : add (white|black|alpha|selection|copy), apply, delete, edit."""
    image = _image(p)
    layer = _layer(image, p)
    action = p.get("action", "add")
    with _undo(image):
        if action == "add":
            if layer.get_mask() is not None:
                _err("ce calque a déjà un masque")
            kind = _enum(Gimp.AddMaskType, p.get("from", "white"))
            mask = layer.create_mask(kind)
            _check(layer.add_mask(mask), "add_mask")
            if p.get("invert"):
                mask.invert(False)
            layer.set_edit_mask(bool(p.get("edit", False)))
        elif action == "apply":
            _check(layer.remove_mask(Gimp.MaskApplyMode.APPLY), "remove_mask")
        elif action == "delete":
            _check(layer.remove_mask(Gimp.MaskApplyMode.DISCARD), "remove_mask")
        elif action == "edit":
            layer.set_edit_mask(bool(p.get("edit", True)))
        elif action == "show":
            layer.set_show_mask(bool(p.get("show", True)))
        else:
            _err("action doit être add, apply, delete, edit ou show")
    return _describe_layer(layer)


# =============================================================================
# Sélections
# =============================================================================

def _selection_context(p):
    """Applique anticrénelage / adoucissement / seuil depuis les paramètres."""
    Gimp.context_set_antialias(bool(p.get("antialias", True)))
    feather = float(p.get("feather", 0.0))
    Gimp.context_set_feather(feather > 0)
    if feather > 0:
        Gimp.context_set_feather_radius(feather, feather)
    if p.get("threshold") is not None:
        Gimp.context_set_sample_threshold(float(p["threshold"]))  # 0..1
    Gimp.context_set_sample_merged(bool(p.get("sample_merged", False)))
    Gimp.context_set_sample_transparent(bool(p.get("sample_transparent", False)))


def op_select(p):
    """Sélection : rectangle, ellipse, polygon, contiguous (baguette), color, path, layer_alpha, all, none, invert."""
    image = _image(p)
    shape = p.get("shape", "rectangle")
    mode = _enum(Gimp.ChannelOps, p.get("mode"), Gimp.ChannelOps.REPLACE)
    with _undo(image):
        Gimp.context_push()
        try:
            _selection_context(p)
            if shape == "rectangle":
                if p.get("radius"):  # coins arrondis
                    _check(image.select_round_rectangle(mode, float(p["x"]), float(p["y"]), float(p["width"]), float(p["height"]),
                                                 float(p["radius"]), float(p["radius"])), "select_round_rectangle")
                else:
                    _check(image.select_rectangle(mode, float(p["x"]), float(p["y"]), float(p["width"]), float(p["height"])), "select_rectangle")
            elif shape == "ellipse":
                _check(image.select_ellipse(mode, float(p["x"]), float(p["y"]), float(p["width"]), float(p["height"])), "select_ellipse")
            elif shape == "polygon":
                pts = [float(v) for pair in p["points"] for v in pair]
                _check(image.select_polygon(mode, pts), "select_polygon")
            elif shape == "contiguous":  # baguette magique
                d = _drawable(image, p)
                _check(image.select_contiguous_color(mode, d, float(p["x"]), float(p["y"])), "select_contiguous_color")
            elif shape == "color":  # tous les pixels de cette couleur
                d = _drawable(image, p)
                _check(image.select_color(mode, d, _color(p["color"])), "select_color")
            elif shape == "path":
                path = _path(image, p)
                _check(image.select_item(mode, path), "select_item")
            elif shape == "layer_alpha":
                layer = _layer(image, p)
                _check(image.select_item(mode, layer), "select_item")
            elif shape == "all":
                Gimp.Selection.all(image)
            elif shape == "none":
                Gimp.Selection.none(image)
            elif shape == "invert":
                Gimp.Selection.invert(image)
            else:
                _err("shape inconnue")
        finally:
            Gimp.context_pop()
    return _selection_info(image)


def _selection_info(image):
    _ok, ok, x1, y1, x2, y2 = Gimp.Selection.bounds(image)  # (succès, non vide, x1, y1, x2, y2)
    return {"empty": not ok, "x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1} if ok else {"empty": True}


def op_selection_modify(p):
    """grow, shrink, border, feather, sharpen, flood (en pixels)."""
    image = _image(p)
    action = p.get("action")
    amount = int(p.get("amount", 0))
    with _undo(image):
        if action == "grow":
            Gimp.Selection.grow(image, amount)
        elif action == "shrink":
            Gimp.Selection.shrink(image, amount)
        elif action == "border":
            Gimp.Selection.border(image, amount)
        elif action == "feather":
            Gimp.Selection.feather(image, float(p.get("amount", 5)))
        elif action == "sharpen":
            Gimp.Selection.sharpen(image)
        elif action == "flood":
            Gimp.Selection.flood(image)
        else:
            _err("action doit être grow, shrink, border, feather, sharpen ou flood")
    return _selection_info(image)


def op_selection_save(p):
    """Sauvegarde la sélection dans un canal nommé (réutilisable avec shape=channel)."""
    image = _image(p)
    with _undo(image):
        channel = Gimp.Selection.save(image)
        channel.set_name(p.get("name", "Sélection"))
    return {"channel_id": channel.get_id(), "name": channel.get_name()}


def op_selection_load(p):
    """Recharge un canal sauvegardé comme sélection."""
    image = _image(p)
    channel = Gimp.Channel.get_by_id(int(p["channel_id"])) if p.get("channel_id") else None
    if channel is None:
        for c in image.get_channels():
            if c.get_name() == p.get("name"):
                channel = c
    if channel is None:
        _err("canal introuvable")
    with _undo(image):
        _check(image.select_item(_enum(Gimp.ChannelOps, p.get("mode"), Gimp.ChannelOps.REPLACE), channel), "select_item")
    return _selection_info(image)


# =============================================================================
# Chemins (vecteurs)
# =============================================================================

def _path(image, p):
    """Chemin désigné par path_id ou path_name."""
    if p.get("path_id") is not None:
        path = PathClass.get_by_id(int(p["path_id"]))
        if path is None:
            _err("chemin introuvable")
        return path
    for v in _image_paths(image):
        if v.get_name() == p.get("path_name"):
            return v
    _err("chemin introuvable (path_id ou path_name)")


def op_path_create(p):
    """Crée un chemin. points : liste de [x, y] (lignes) ou [x,y, cx1,cy1, cx2,cy2] (courbes de Bézier)."""
    image = _image(p)
    with _undo(image):
        path = PathClass.new(image, p.get("name", "Chemin"))
        insert = getattr(image, "insert_path", None) or getattr(image, "insert_vectors")
        insert(path, None, 0)
        pts = p["points"]
        first = pts[0]
        stroke = path.bezier_stroke_new_moveto(float(first[0]), float(first[1]))
        for pt in pts[1:]:
            if len(pt) >= 6:
                # pt = [x, y, c1x, c1y, c2x, c2y] : point d'arrivée + ses 2 poignées
                path.bezier_stroke_cubicto(stroke, float(pt[2]), float(pt[3]), float(pt[4]), float(pt[5]), float(pt[0]), float(pt[1]))
            else:
                path.bezier_stroke_lineto(stroke, float(pt[0]), float(pt[1]))
        if p.get("close", False):
            path.stroke_close(stroke)
    return {"path_id": path.get_id(), "name": path.get_name()}


def op_path_stroke(p):
    """Trace un chemin (ou la sélection) avec une ligne de couleur/épaisseur données."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        Gimp.context_push()
        try:
            Gimp.context_set_foreground(_color(p.get("color"), "#000000"))
            Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
            Gimp.context_set_line_width(float(p.get("width", 3.0)))
            Gimp.context_set_line_width_unit(Gimp.Unit.pixel())
            Gimp.context_set_line_cap_style(_enum(Gimp.CapStyle, p.get("cap"), Gimp.CapStyle.ROUND))
            Gimp.context_set_line_join_style(_enum(Gimp.JoinStyle, p.get("join"), Gimp.JoinStyle.ROUND))
            Gimp.context_set_antialias(True)
            Gimp.context_set_opacity(float(p.get("opacity", 100.0)))
            if p.get("target_selection"):
                _check(d.edit_stroke_selection(), "edit_stroke_selection")
            else:
                _check(d.edit_stroke_item(_path(image, p)), "edit_stroke_item")
        finally:
            Gimp.context_pop()
    return _describe_layer(_layer(image, p))


# =============================================================================
# Peinture / remplissage
# =============================================================================

def op_set_colors(p):
    """Couleurs de premier plan / arrière-plan du contexte GIMP."""
    if p.get("foreground"):
        Gimp.context_set_foreground(_color(p["foreground"]))
    if p.get("background"):
        Gimp.context_set_background(_color(p["background"]))
    return {"foreground": _color_to_hex(Gimp.context_get_foreground()),
            "background": _color_to_hex(Gimp.context_get_background())}


def op_fill(p):
    """Remplit la sélection (ou tout le calque) : couleur, pattern, ou clear (transparent)."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        Gimp.context_push()
        try:
            Gimp.context_set_opacity(float(p.get("opacity", 100.0)))
            Gimp.context_set_paint_mode(_enum(Gimp.LayerMode, p.get("mode"), Gimp.LayerMode.NORMAL))
            if p.get("clear"):
                _check(d.edit_clear(), "edit_clear")
            elif p.get("pattern"):
                pattern = Gimp.Pattern.get_by_name(p["pattern"])
                if pattern is None:
                    _err(f"motif « {p['pattern']} » introuvable")
                Gimp.context_set_pattern(pattern)
                _check(d.edit_fill(Gimp.FillType.PATTERN), "edit_fill")
            else:
                Gimp.context_set_foreground(_color(p.get("color"), "#000000"))
                _check(d.edit_fill(Gimp.FillType.FOREGROUND), "edit_fill")
        finally:
            Gimp.context_pop()
    return _describe_layer(_layer(image, p))


def op_bucket_fill(p):
    """Pot de peinture : remplit la zone contiguë (ou similaire) autour de (x, y)."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        Gimp.context_push()
        try:
            Gimp.context_set_foreground(_color(p.get("color"), "#000000"))
            Gimp.context_set_opacity(float(p.get("opacity", 100.0)))
            Gimp.context_set_sample_threshold(float(p.get("threshold", 0.15)))
            Gimp.context_set_sample_merged(bool(p.get("sample_merged", False)))
            Gimp.context_set_antialias(True)
            _check(d.edit_bucket_fill(Gimp.FillType.FOREGROUND, float(p["x"]), float(p["y"])), "edit_bucket_fill")
        finally:
            Gimp.context_pop()
    return _describe_layer(_layer(image, p))


def op_gradient(p):
    """Dégradé linéaire/radial... entre (x1,y1) et (x2,y2), couleurs from -> to."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        Gimp.context_push()
        try:
            Gimp.context_set_foreground(_color(p.get("from"), "#000000"))
            Gimp.context_set_background(_color(p.get("to"), "#ffffff"))
            if p.get("gradient"):
                g = Gimp.Gradient.get_by_name(p["gradient"])
                if g is None:
                    _err(f"dégradé « {p['gradient']} » introuvable")
                Gimp.context_set_gradient(g)
            else:
                Gimp.context_set_gradient_fg_bg_rgb()
            Gimp.context_set_gradient_blend_color_space(Gimp.GradientBlendColorSpace.RGB_PERCEPTUAL)
            Gimp.context_set_gradient_repeat_mode(_enum(Gimp.RepeatMode, p.get("repeat"), Gimp.RepeatMode.NONE))
            Gimp.context_set_gradient_reverse(bool(p.get("reverse", False)))
            Gimp.context_set_opacity(float(p.get("opacity", 100.0)))
            Gimp.context_set_paint_mode(_enum(Gimp.LayerMode, p.get("mode"), Gimp.LayerMode.NORMAL))
            _check(d.edit_gradient_fill(_enum(Gimp.GradientType, p.get("shape"), Gimp.GradientType.LINEAR),
                                 float(p.get("offset", 0.0)), False, 1, 0.0, True,
                                 float(p["x1"]), float(p["y1"]), float(p["x2"]), float(p["y2"])), "edit_gradient_fill")
        finally:
            Gimp.context_pop()
    return _describe_layer(_layer(image, p))


def op_paint(p):
    """Coup de pinceau libre : tool = paintbrush|pencil|airbrush|eraser|smudge|dodge|burn|clone."""
    image = _image(p)
    d = _drawable(image, p)
    pts = [float(v) for pair in p["points"] for v in pair]
    if len(pts) < 2:
        _err("points doit contenir au moins un [x, y]")
    if len(pts) == 2:  # un seul point : on double pour obtenir une touche
        pts = pts + pts
    tool = p.get("tool", "paintbrush")
    with _undo(image):
        Gimp.context_push()
        try:
            if p.get("brush"):
                brush = Gimp.Brush.get_by_name(p["brush"])
                if brush is None:
                    _err(f"brosse « {p['brush']} » introuvable (voir resources_list)")
                Gimp.context_set_brush(brush)
            Gimp.context_set_brush_size(float(p.get("size", 20.0)))
            Gimp.context_set_brush_hardness(float(p.get("hardness", 0.5)))
            Gimp.context_set_brush_force(float(p.get("force", 0.5)))
            Gimp.context_set_brush_aspect_ratio(float(p.get("aspect", 0.0)))
            Gimp.context_set_brush_angle(float(p.get("angle", 0.0)))
            Gimp.context_set_opacity(float(p.get("opacity", 100.0)))
            Gimp.context_set_paint_mode(_enum(Gimp.LayerMode, p.get("mode"), Gimp.LayerMode.NORMAL))
            if p.get("color"):
                Gimp.context_set_foreground(_color(p["color"]))
            if tool == "paintbrush":
                _check(Gimp.paintbrush_default(d, pts), "paintbrush_default")
            elif tool == "pencil":
                _check(Gimp.pencil(d, pts), "pencil")
            elif tool == "airbrush":
                _check(Gimp.airbrush(d, float(p.get("pressure", 50.0)), pts), "airbrush")
            elif tool == "eraser":
                _check(Gimp.eraser_default(d, pts), "eraser_default")
            elif tool == "smudge":
                _check(Gimp.smudge(d, float(p.get("pressure", 50.0)), pts), "smudge")
            elif tool in ("dodge", "burn"):
                _check(Gimp.dodgeburn_default(d, pts), "dodgeburn_default")
            elif tool == "clone":
                src = _layer(image, p, "source_layer_id", "source_layer_name")
                _check(Gimp.clone(d, src, Gimp.CloneType.IMAGE, float(p["source_x"]), float(p["source_y"]), pts), "clone")
            else:
                _err("tool inconnu")
        finally:
            Gimp.context_pop()
    return _describe_layer(_layer(image, p))


def op_text_add(p):
    """Calque de texte : texte, police, taille (px), couleur, position, justification."""
    image = _image(p)
    with _undo(image):
        font = Gimp.Font.get_by_name(p["font"]) if p.get("font") else None
        if font is None:
            font = Gimp.context_get_font()
        layer = Gimp.TextLayer.new(image, p["text"], font, float(p.get("size", 48)), Gimp.Unit.pixel())
        parent, position = _parent_and_position(image, p)
        _check(image.insert_layer(layer, parent, position), "insert_layer")
        if p.get("name"):
            layer.set_name(p["name"])
        layer.set_color(_color(p.get("color"), "#000000"))
        layer.set_antialias(True)
        if p.get("justify"):
            layer.set_justification(_enum(Gimp.TextJustification, p["justify"]))
        if p.get("letter_spacing") is not None:
            layer.set_letter_spacing(float(p["letter_spacing"]))
        if p.get("line_spacing") is not None:
            layer.set_line_spacing(float(p["line_spacing"]))
        if p.get("width"):  # boîte de texte fixe (retour à la ligne automatique)
            _check(layer.resize(float(p["width"]), float(p.get("height", layer.get_height()))), "resize")
        _check(layer.set_offsets(int(p.get("x", 0)), int(p.get("y", 0))), "set_offsets")
        if p.get("opacity") is not None:
            layer.set_opacity(float(p["opacity"]))
        if p.get("mode"):
            layer.set_mode(_enum(Gimp.LayerMode, p["mode"]))
        image.set_selected_layers([layer])
    info = _describe_layer(layer)
    info["font_used"] = font.get_name()
    return info


def op_text_edit(p):
    """Modifie un calque de texte existant."""
    image = _image(p)
    layer = _layer(image, p)
    if not layer.is_text_layer():
        _err("ce calque n'est pas un calque de texte")
    with _undo(image):
        if p.get("text") is not None:
            layer.set_text(p["text"])
        if p.get("font"):
            font = Gimp.Font.get_by_name(p["font"])
            if font is None:
                _err(f"police « {p['font']} » introuvable")
            layer.set_font(font)
        if p.get("size") is not None:
            layer.set_font_size(float(p["size"]), Gimp.Unit.pixel())
        if p.get("color"):
            layer.set_color(_color(p["color"]))
        if p.get("justify"):
            layer.set_justification(_enum(Gimp.TextJustification, p["justify"]))
        if p.get("x") is not None or p.get("y") is not None:
            ok, ox, oy = layer.get_offsets()
            _check(layer.set_offsets(int(p.get("x", ox)), int(p.get("y", oy))), "set_offsets")
    return _describe_layer(layer)


def op_resources_list(p):
    """Liste les polices, brosses, motifs, dégradés ou palettes (filtre optionnel)."""
    kind = p.get("kind", "fonts")
    flt = p.get("filter", "") or ""
    getters = {"fonts": "fonts_get_list", "brushes": "brushes_get_list", "patterns": "patterns_get_list",
               "gradients": "gradients_get_list", "palettes": "palettes_get_list"}
    if kind not in getters:
        _err("kind doit être fonts, brushes, patterns, gradients ou palettes")
    items = getattr(Gimp, getters[kind])(flt)
    names = [i.get_name() if hasattr(i, "get_name") else str(i) for i in items]
    limit = int(p.get("limit", 200))
    return {"count": len(names), "names": names[:limit]}


# =============================================================================
# Couleur / ajustements (API Drawable de GIMP, appliquée de façon destructive)
# NB : ces méthodes sont marquées « dépréciées » en 3.2 au profit des filtres
# "gimp:levels", "gimp:curves", etc. Elles fonctionnent toujours ; la migration
# vers _apply_filter(d, "gimp:...", params) est prévue.
# =============================================================================

def op_adjust(p):
    """Ajustements : brightness_contrast, levels, curves, hue_saturation, color_balance,
    colorize, desaturate, invert, threshold, posterize, auto_levels, shadows_highlights."""
    image = _image(p)
    d = _drawable(image, p)
    kind = p.get("kind")
    with _undo(image):
        if kind == "brightness_contrast":
            # brightness / contrast en -100..100 (convertis en -1..1)
            _check(d.brightness_contrast(float(p.get("brightness", 0)) / 100.0, float(p.get("contrast", 0)) / 100.0), "brightness_contrast")
        elif kind == "levels":
            channel = _enum(Gimp.HistogramChannel, p.get("channel"), Gimp.HistogramChannel.VALUE)
            _check(d.levels(channel, float(p.get("low_in", 0)) / 255.0, float(p.get("high_in", 255)) / 255.0, True,
                     float(p.get("gamma", 1.0)), float(p.get("low_out", 0)) / 255.0, float(p.get("high_out", 255)) / 255.0, True), "levels")
        elif kind == "curves":
            # points : [[x, y], ...] en 0..255, sur le canal choisi
            channel = _enum(Gimp.HistogramChannel, p.get("channel"), Gimp.HistogramChannel.VALUE)
            pts = [float(v) / 255.0 for pair in p["points"] for v in pair]
            _check(d.curves_spline(channel, pts), "curves_spline")
        elif kind == "hue_saturation":
            _check(d.hue_saturation(_enum(Gimp.HueRange, p.get("range"), Gimp.HueRange.ALL), float(p.get("hue", 0)),
                             float(p.get("lightness", 0)), float(p.get("saturation", 0)), float(p.get("overlap", 0))), "hue_saturation")
        elif kind == "color_balance":
            _check(d.color_balance(_enum(Gimp.TransferMode, p.get("range"), Gimp.TransferMode.MIDTONES), bool(p.get("preserve_luminosity", True)),
                            float(p.get("cyan_red", 0)), float(p.get("magenta_green", 0)), float(p.get("yellow_blue", 0))), "color_balance")
        elif kind == "colorize":
            _check(d.colorize_hsl(float(p.get("hue", 180)), float(p.get("saturation", 50)), float(p.get("lightness", 0))), "colorize_hsl")
        elif kind == "desaturate":
            _check(d.desaturate(_enum(Gimp.DesaturateMode, p.get("mode"), Gimp.DesaturateMode.LUMINANCE)), "desaturate")
        elif kind == "invert":
            d.invert(bool(p.get("linear", False)))
        elif kind == "threshold":
            _check(d.threshold(_enum(Gimp.HistogramChannel, p.get("channel"), Gimp.HistogramChannel.VALUE),
                        float(p.get("low", 127)) / 255.0, float(p.get("high", 255)) / 255.0), "threshold")
        elif kind == "posterize":
            _check(d.posterize(int(p.get("levels", 4))), "posterize")
        elif kind == "auto_levels":
            _check(d.levels_stretch(), "levels_stretch")
        elif kind == "shadows_highlights":
            _check(d.shadows_highlights(float(p.get("shadows", 0)), float(p.get("highlights", 0)), float(p.get("whitepoint", 0)),
                                 float(p.get("radius", 100)), float(p.get("compress", 50)), float(p.get("shadows_ccorrect", 100)),
                                 float(p.get("highlights_ccorrect", 50))), "shadows_highlights")
        else:
            _err("kind d'ajustement inconnu")
    return _describe_layer(_layer(image, p))


# =============================================================================
# Filtres GEGL (générique, destructif ou non destructif)
# =============================================================================

# Règles reprises de GIMP 3.2 (app/gegl/gimp-gegl-utils.c, gimp_gegl_op_blacklisted) :
# GIMP refuse ces opérations dans gimp-drawable-filter-new (« The filter is hidden »).
_HIDDEN_CATEGORIES = {"compositors", "core", "debug", "display", "hidden", "input",
                      "output", "programming", "transform", "video"}
_HIDDEN_NAMES = {
    "gegl:color", "gegl:contrast-curve", "gegl:convert-format", "gegl:ditto", "gegl:fill-path",
    "gegl:gray", "gegl:hstack", "gegl:introspect", "gegl:json:dropshadow2", "gegl:json:grey2",
    "gegl:layer", "gegl:lcms-from-profile", "gegl:linear-gradient", "gegl:map-absolute",
    "gegl:map-relative", "gegl:matting-global", "gegl:matting-levin", "gegl:nop", "gegl:opacity",
    "gegl:pack", "gegl:path", "gegl:posterize", "gegl:radial-gradient", "gegl:rectangle",
    "gegl:seamless-clone", "gegl:text", "gegl:threshold", "gegl:tile", "gegl:unpremul",
    "gegl:vector-stroke", "gegl:wavelet-blur", "gegl:gegl",
}
# Équivalents à utiliser automatiquement (même effet, version GIMP autorisée)
_REPLACEMENTS = {"gegl:threshold": "gimp:threshold", "gegl:posterize": "gimp:posterize",
                 "gegl:gray": "gimp:desaturate"}
# Conseils quand il n'y a pas d'équivalent direct
_ADVICE = {"gegl:linear-gradient": "utilise l'outil gimp_gradient",
           "gegl:radial-gradient": "utilise l'outil gimp_gradient shape=\"radial\"",
           "gegl:text": "utilise l'outil gimp_text",
           "gegl:rectangle": "utilise gimp_select puis gimp_fill",
           "gegl:path": "utilise gimp_path_create puis gimp_stroke",
           "gegl:vector-stroke": "utilise gimp_path_create puis gimp_stroke"}
# Opérations propres à GIMP (non listées par GEGL dans le processus du plugin)
_GIMP_OPS = {
    "gimp:brightness-contrast": "Luminosité-contraste", "gimp:color-balance": "Balance des couleurs",
    "gimp:colorize": "Colorier", "gimp:curves": "Courbes", "gimp:desaturate": "Désaturer",
    "gimp:hue-saturation": "Teinte-saturation", "gimp:levels": "Niveaux", "gimp:offset": "Décalage",
    "gimp:posterize": "Postériser", "gimp:threshold": "Seuil", "gimp:semi-flatten": "Semi-aplatir",
    "gimp:set-alpha": "Définir l'alpha", "gimp:threshold-alpha": "Seuil alpha",
}


def _filter_status(name):
    """(autorisé, raison) pour une opération, selon les règles de GIMP 3.2."""
    if name in _GIMP_OPS:
        return True, ""
    if name.startswith("gimp:"):
        return False, "opération interne de GIMP non utilisable comme filtre"
    if name in _HIDDEN_NAMES:
        return False, "GIMP la masque (il utilise sa propre version)"
    try:
        categories = Gegl.Operation.get_key(name, "categories") or ""
    except Exception:  # noqa: BLE001
        categories = ""
    hidden = _HIDDEN_CATEGORIES.intersection(categories.split(":"))
    if hidden and name != "gimp:offset":
        return False, f"catégorie « {sorted(hidden)[0]} » masquée par GIMP"
    return True, ""


def _gegl_ops():
    try:
        Gegl.init(None)
    except Exception:  # noqa: BLE001 — déjà initialisé dans le processus GIMP
        pass
    return list(Gegl.list_operations())


def op_filter_search(p):
    """Recherche une opération utilisable comme filtre (GEGL autorisées + gimp:*) par mot-clé."""
    q = (p.get("query") or "").lower()
    results = []
    # Opérations GIMP (couleur) d'abord : ce sont les versions recommandées
    for name, title in _GIMP_OPS.items():
        if q in name.lower() or q in title.lower():
            results.append({"name": name, "title": title, "description": "opération GIMP"})
    for name in _gegl_ops():
        allowed, _reason = _filter_status(name)
        if not allowed:
            continue  # GIMP refuserait ce filtre : inutile de le proposer
        title = Gegl.Operation.get_key(name, "title") or ""
        desc = Gegl.Operation.get_key(name, "description") or ""
        if q in name.lower() or q in title.lower() or q in desc.lower():
            results.append({"name": name, "title": title, "description": desc[:200]})
    return {"count": len(results), "operations": results[:int(p.get("limit", 60))]}


def op_filter_describe(p):
    """Propriétés (nom, type, bornes, défaut, choix) d'une opération utilisable comme filtre."""
    name = _resolve_filter_name(p["name"])
    if not name.startswith("gimp:"):
        props = Gegl.Operation.list_properties(name)
        return {"name": name, "title": Gegl.Operation.get_key(name, "title") or "",
                "description": Gegl.Operation.get_key(name, "description") or "",
                "properties": [_pspec_info(ps) for ps in props]}
    # Les opérations gimp:* n'existent que dans le processus GIMP : on crée un
    # filtre temporaire (jamais appliqué) pour lire sa configuration.
    image = _image(p)
    flt = _check(Gimp.DrawableFilter.new(_layer(image, p), name, "pinceau-describe"),
                 f"création du filtre {name}")
    try:
        props = [_pspec_info(ps) for ps in flt.get_config().list_properties()]
    finally:
        flt.delete()
    return {"name": name, "title": _GIMP_OPS.get(name, ""), "description": "opération GIMP",
            "properties": props}


def _resolve_filter_name(name):
    """Remplace une opération masquée par son équivalent GIMP, ou lève une erreur claire."""
    if name in _REPLACEMENTS:
        return _REPLACEMENTS[name]
    allowed, reason = _filter_status(name)
    if not allowed:
        hint = _ADVICE.get(name, "cherche une alternative avec gimp_filter_search")
        _err(f"« {name} » n'est pas utilisable dans GIMP 3.2 ({reason}) : {hint}.")
    return name


def _apply_filter(d, op_name, params, label=None, opacity=100.0, mode=None, non_destructive=False):
    """Crée un GimpDrawableFilter et l'applique (fusionné) ou l'attache (non destructif)."""
    op_name = _resolve_filter_name(op_name)
    flt = _check(Gimp.DrawableFilter.new(d, op_name, label or op_name), f"création du filtre {op_name}")
    cfg = flt.get_config()
    for key, value in (params or {}).items():
        pspec = cfg.find_property(key)
        if pspec is None:
            valid = [ps.name for ps in cfg.list_properties()]
            _err(f"propriété « {key} » inconnue pour {op_name} ; propriétés : {', '.join(valid)}")
        cfg.set_property(key, _json_to_gvalue(value, pspec))
    flt.set_opacity(float(opacity) / 100.0)
    if mode:
        flt.set_blend_mode(_enum(Gimp.LayerMode, mode))
    if non_destructive:
        d.append_filter(flt)
        flt.update()
    else:
        d.merge_filter(flt)
    return flt


def op_filter_apply(p):
    """Applique une opération GEGL (ex. gegl:gaussian-blur, gegl:unsharp-mask, gegl:dropshadow)."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        flt = _apply_filter(d, p["name"], p.get("params"), p.get("label"), p.get("opacity", 100.0),
                            p.get("mode"), bool(p.get("non_destructive", False)))
    info = _describe_layer(_layer(image, p))
    info["filter"] = flt.get_operation_name()  # peut différer du nom demandé (équivalent GIMP)
    if p.get("non_destructive"):
        info["filter_id"] = flt.get_id()
    return info


def op_filter_list_on_layer(p):
    """Liste les effets non destructifs attachés à un calque."""
    image = _image(p)
    d = _drawable(image, p)
    out = []
    for f in d.get_filters():
        out.append({"id": f.get_id(), "name": f.get_name(), "operation": f.get_operation_name(),
                    "visible": f.get_visible(), "opacity": f.get_opacity()})
    return out


def op_filter_remove(p):
    """Supprime un effet non destructif (par id) ou les fusionne tous (merge_all)."""
    image = _image(p)
    d = _drawable(image, p)
    with _undo(image):
        if p.get("merge_all"):
            d.merge_filters()
            return {"merged": True}
        for f in d.get_filters():
            if f.get_id() == int(p["filter_id"]):
                f.delete()
                return {"removed": p["filter_id"]}
    _err("filtre introuvable sur ce calque")


# =============================================================================
# PDB (base de procédures de GIMP) — découverte et appel
# =============================================================================

def op_pdb_search(p):
    """Recherche des procédures du PDB par expression régulière sur le nom ou la description."""
    pdb = Gimp.get_pdb()
    q = p.get("query", ".*")
    names = set(pdb.query_procedures(q, "", "", "", "", "", "", ""))
    if p.get("in_blurb", True):
        names |= set(pdb.query_procedures("", q, "", "", "", "", "", ""))
    names = sorted(names)
    out = []
    for n in names[:int(p.get("limit", 60))]:
        proc = pdb.lookup_procedure(n)
        out.append({"name": n, "blurb": proc.get_blurb() if proc else ""})
    return {"count": len(names), "procedures": out}


def op_pdb_describe(p):
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(p["name"])
    if proc is None:
        _err(f"procédure « {p['name']} » introuvable")
    return {"name": p["name"], "blurb": proc.get_blurb(), "help": proc.get_help(),
            "arguments": [_pspec_info(a) for a in proc.get_arguments()],
            "returns": [_pspec_info(r) for r in proc.get_return_values()]}


def op_pdb_call(p):
    """Appelle une procédure du PDB avec des arguments JSON (ids pour images/calques)."""
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(p["name"])
    if proc is None:
        _err(f"procédure « {p['name']} » introuvable")
    cfg = proc.create_config()
    args = p.get("args") or {}
    arg_specs = {a.name: a for a in proc.get_arguments()}
    for key, value in args.items():
        if key not in arg_specs:
            _err(f"argument « {key} » inconnu ; arguments : {', '.join(arg_specs)}")
        cfg.set_property(key, _json_to_gvalue(value, arg_specs[key]))
    if "run-mode" in arg_specs and "run-mode" not in args:
        cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    image = None
    if "image" in args:
        image = Gimp.Image.get_by_id(int(args["image"]))
    if image is not None:
        image.undo_group_start()
    try:
        result = proc.run(cfg)
    finally:
        if image is not None:
            image.undo_group_end()
        Gimp.displays_flush()
    status = result.index(0)
    values = [_gvalue_to_json(result.index(i)) for i in range(1, result.length())]
    if status != Gimp.PDBStatusType.SUCCESS:
        _err(f"la procédure a échoué : {status.value_nick} {values}")
    return {"status": status.value_nick, "values": values}


# =============================================================================
# Python arbitraire (couverture totale)
# =============================================================================

def _namespace():
    """Espace de noms persistant : imports et variables survivent entre appels."""
    global PY_NAMESPACE
    if PY_NAMESPACE is None:
        PY_NAMESPACE = {
            "Gimp": Gimp, "Gegl": Gegl, "Gio": Gio, "GLib": GLib, "GObject": GObject,
            "pdb": Gimp.get_pdb(), "ops": globals(), "STATE": STATE,
            "image": lambda image_id=None: _image({"image_id": image_id}),
            "layer": lambda image, **kw: _layer(image, kw),
            "color": _color,
        }
    return PY_NAMESPACE


def op_run_python(p):
    """Exécute du code Python dans GIMP. mode=exec (stdout capturé, variable `result`) ou eval."""
    code = p.get("code", "")
    ns = _namespace()
    buf = io.StringIO()
    image = None
    if p.get("undo_group", True):
        try:
            image = _image({})
        except ValueError:
            image = None
    if image is not None:
        image.undo_group_start()
    try:
        with contextlib.redirect_stdout(buf):
            if p.get("mode") == "eval":
                value = eval(code, ns)  # noqa: S307 — c'est le but de l'outil
            else:
                ns.pop("result", None)
                exec(code, ns)  # noqa: S102
                value = ns.get("result")
    finally:
        if image is not None:
            image.undo_group_end()
        Gimp.displays_flush()
    return {"stdout": buf.getvalue(), "result": _gvalue_to_json(value)}


# =============================================================================
# Dispatcher
# =============================================================================

OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_") and callable(fn)}


def dispatch(cmd, params):
    """Point d'entrée appelé par pinceau.py."""
    fn = OPS.get(cmd)
    if fn is None:
        raise ValueError(f"commande inconnue « {cmd} » ; commandes : {', '.join(sorted(OPS))}")
    return fn(params or {})
