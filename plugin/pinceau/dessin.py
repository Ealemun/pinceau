# =============================================================================
# volumes3d.py : mini-moteur de construction 3D pour dessiner en perspective
# avec Pinceau (à envoyer tel quel dans gimp_run_python, une fois par session).
#
# Idée : un dessinateur ne trace pas des cercles à plat, il imagine des volumes
# (sphères, boîtes, cylindres) tournés dans l'espace, vus par une caméra. Ce
# module fait exactement ça : on place des volumes en 3D, on choisit l'angle de
# vue, et il trace leur silhouette, leurs contours croisés (lignes qui
# s'enroulent autour de la forme) et gère les recouvrements (le volume devant
# cache celui derrière). Le résultat sert de calque Construction, sur lequel on
# dessine ensuite le vrai trait.
#
# Repère monde : x vers la droite, y vers le haut, z vers le fond.
# Unités : pixels (à la distance de la caméra, 1 unité = 1 px à l'écran).
# L'espace de noms de gimp_run_python fournit Gimp, color(), image(), layer().
# =============================================================================
import math
import random

CAM = {}  # réglages de la caméra courante (remplis par camera())


def camera(cx, cy, rotation=35.0, plongee=15.0, dist=2500.0):
    """Règle l'angle de vue.
    cx, cy   : point de l'image où se projette l'origine du monde (centre du sujet).
    rotation : degrés ; le sujet tourne sur lui-même (0 = de face, 35 = trois-quarts,
               90 = profil). Positif : son avant part vers la gauche de l'image.
    plongee  : degrés ; positif = on voit le sujet un peu du dessus, négatif = contre-plongée.
    dist     : distance caméra-sujet en px ; petite (1200) = perspective forte et dynamique,
               grande (6000) = presque à plat. 2000-3000 convient à la plupart des sujets."""
    CAM.update(cx=float(cx), cy=float(cy), yaw=math.radians(rotation),
               pitch=math.radians(-plongee), dist=float(dist), f=float(dist))


def _rot(v, yaw=0.0, pitch=0.0, roll=0.0):
    """Tourne un vecteur (x, y, z) : roulis (axe Z), puis tangage (axe X), puis lacet (axe Y)."""
    x, y, z = v
    c, s = math.cos(roll), math.sin(roll)
    x, y = x * c - y * s, x * s + y * c
    c, s = math.cos(pitch), math.sin(pitch)
    y, z = y * c - z * s, y * s + z * c
    c, s = math.cos(yaw), math.sin(yaw)
    x, z = x * c + z * s, -x * s + z * c
    return (x, y, z)


def _to_cam(v, translate=True):
    """Passe un point (ou une normale si translate=False) du monde au repère caméra."""
    x, y, z = v
    c, s = math.cos(CAM["yaw"]), math.sin(CAM["yaw"])
    x, z = x * c + z * s, -x * s + z * c
    c, s = math.cos(CAM["pitch"]), math.sin(CAM["pitch"])
    y, z = y * c - z * s, y * s + z * c
    return (x, y, z + (CAM["dist"] if translate else 0.0))


def _proj(pc):
    """Projette un point caméra sur l'image (perspective) : renvoie [x, y] en pixels."""
    x, y, z = pc
    k = CAM["f"] / z
    return [CAM["cx"] + x * k, CAM["cy"] - y * k]


def projeter(p):
    """Projette un point du monde (x, y, z) en pixels image : utile pour placer à la main
    un œil, une griffe, une ligne de sol au bon endroit en perspective."""
    return _proj(_to_cam(p))


def _visible(pc, nc):
    """Une surface est tournée vers la caméra si sa normale pointe vers elle."""
    return nc[0] * pc[0] + nc[1] * pc[1] + nc[2] * pc[2] < 0


def _hull(pts):
    """Enveloppe convexe (algorithme de la chaîne monotone) : contour extérieur d'un volume convexe."""
    pts = sorted(set((round(p[0], 2), round(p[1], 2)) for p in pts))
    if len(pts) < 3:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def _runs(samples):
    """Découpe une suite de (point2D, visible) en morceaux continus visibles / cachés."""
    runs, cur, vis = [], [], None
    for p, v in samples:
        if vis is None or v == vis:
            cur.append(p)
        else:
            cur.append(p)  # chevauchement d'un point : pas de trou entre deux morceaux
            runs.append((cur, vis))
            cur = [p]
        vis = v
    if len(cur) > 1:
        runs.append((cur, vis))
    return runs


def _volume(nom, silhouette3d, lignes, centre):
    """Assemble la description d'un volume prête à dessiner."""
    zc = _to_cam(centre)[2]
    return {"nom": nom, "silhouette": _hull([_proj(_to_cam(p)) for p in silhouette3d]),
            "lignes": lignes, "z": zc, "k": CAM["f"] / zc}


def ellipsoide(nom, centre, rayons, rot=(0, 0, 0), anneaux=3, meridiens=2):
    """Sphère ou œuf : tête, thorax, bassin, museau arrondi, queue touffue, joue.
    centre (x, y, z), rayons (rx, ry, rz), rot = (lacet, tangage, roulis) en degrés.
    anneaux / meridiens : nombre de contours croisés tracés (ce sont eux qui donnent le volume)."""
    a, b, c = rayons
    r = [math.radians(v) for v in rot]
    surf = []
    for i in range(1, 24):
        phi = -math.pi / 2 + math.pi * i / 24
        for j in range(48):
            th = 2 * math.pi * j / 48
            surf.append(_place((a * math.cos(phi) * math.cos(th), b * math.sin(phi),
                                c * math.cos(phi) * math.sin(th)), centre, r))

    def pt(phi, th):
        loc = (a * math.cos(phi) * math.cos(th), b * math.sin(phi), c * math.cos(phi) * math.sin(th))
        nl = (loc[0] / (a * a), loc[1] / (b * b), loc[2] / (c * c))
        pc = _to_cam(_place(loc, centre, r))
        nc = _to_cam(_rot(nl, *r), translate=False)
        return (_proj(pc), _visible(pc, nc))

    lignes = []
    for i in range(1, anneaux + 1):  # parallèles (anneaux horizontaux)
        phi = -math.pi / 2 + math.pi * i / (anneaux + 1)
        lignes += _runs([pt(phi, 2 * math.pi * j / 64) for j in range(65)])
    for m in range(meridiens):  # méridiens (de pôle à pôle)
        th = math.pi * m / max(1, meridiens)
        lignes += _runs([pt(-math.pi / 2 + math.pi * j / 64, th) for j in range(65)])
        lignes += _runs([pt(-math.pi / 2 + math.pi * j / 64, th + math.pi) for j in range(65)])
    vol = _volume(nom, surf, lignes, centre)
    vol.update(centre=centre, rayons=rayons, rot=rot)  # géométrie gardée : oeil() s'en sert
    return vol


def par_nom(volumes, nom):
    """Retrouve un volume par son nom dans la liste renvoyée par construire()."""
    for v in volumes:
        if v["nom"] == nom:
            return v
    raise KeyError(f"aucun volume nommé « {nom} »")


def boite(nom, centre, taille, rot=(0, 0, 0)):
    """Boîte : bassin, cage thoracique anguleuse, museau carré, objet, bâtiment.
    taille (largeur, hauteur, profondeur)."""
    w, h, d = (t / 2.0 for t in taille)
    r = [math.radians(v) for v in rot]
    coins = [(sx * w, sy * h, sz * d) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    faces = {  # normale locale -> indices des 4 coins
        (1, 0, 0): [4, 5, 7, 6], (-1, 0, 0): [0, 1, 3, 2], (0, 1, 0): [2, 3, 7, 6],
        (0, -1, 0): [0, 1, 5, 4], (0, 0, 1): [1, 3, 7, 5], (0, 0, -1): [0, 2, 6, 4]}
    monde = [_place(cn, centre, r) for cn in coins]
    vis_face = {}
    for n, idx in faces.items():
        pc = _to_cam(monde[idx[0]])
        vis_face[n] = _visible(pc, _to_cam(_rot(n, *r), translate=False))
    lignes, faites = [], set()
    for n, idx in faces.items():
        for k in range(4):
            e = tuple(sorted((idx[k], idx[(k + 1) % 4])))
            if e in faites:
                continue
            faites.add(e)
            vis = any(vis_face[m] for m, ii in faces.items() if e[0] in ii and e[1] in ii)
            lignes.append(([_proj(_to_cam(monde[e[0]])), _proj(_to_cam(monde[e[1]]))], vis))
    return _volume(nom, monde, lignes, centre)


def cylindre(nom, centre, rayon, hauteur, rot=(0, 0, 0), rayon_haut=None, anneaux=1):
    """Cylindre (ou tronc de cône si rayon_haut) : cou, patte, bras, queue fine, tronc, verre.
    L'axe local est vertical (y) : `rayon` est en bas, `rayon_haut` en haut. Incline-le avec rot :
    tangage NÉGATIF = le haut part vers l'avant (z négatif, vers le spectateur avant rotation de
    la caméra) ; tangage positif = vers l'arrière ; roulis = penche à gauche/droite.
    Ex. museau ou bec qui pointe vers l'avant : rot=(0, -75, 0), rayon_haut petit.
    Vérifie toujours sur l'aperçu que la pointe d'un cône part du bon côté."""
    r0, r1 = float(rayon), float(rayon if rayon_haut is None else rayon_haut)
    r = [math.radians(v) for v in rot]
    h = hauteur / 2.0
    surf, lignes = [], []
    for y, rr in ((-h, r0), (h, r1)):
        for j in range(48):
            th = 2 * math.pi * j / 48
            surf.append(_place((rr * math.cos(th), y, rr * math.sin(th)), centre, r))
    # Anneaux (extrémités + intermédiaires) : visibles côté face, cachés côté dos.
    cap_bas = _visible(_to_cam(_place((0, -h, 0), centre, r)), _to_cam(_rot((0, -1, 0), *r), translate=False))
    cap_haut = _visible(_to_cam(_place((0, h, 0), centre, r)), _to_cam(_rot((0, 1, 0), *r), translate=False))
    niveaux = [(-h, r0, cap_bas)] + [(-h + 2 * h * i / (anneaux + 1),
                                      r0 + (r1 - r0) * i / (anneaux + 1), False) for i in range(1, anneaux + 1)] + [(h, r1, cap_haut)]
    for y, rr, cap in niveaux:
        smp = []
        for j in range(65):
            th = 2 * math.pi * j / 64
            loc = (rr * math.cos(th), y, rr * math.sin(th))
            pc = _to_cam(_place(loc, centre, r))
            nc = _to_cam(_rot((math.cos(th), 0, math.sin(th)), *r), translate=False)
            smp.append((_proj(pc), cap or _visible(pc, nc)))
        lignes += _runs(smp)
    return _volume(nom, surf, lignes, centre)


def _place(local, centre, r):
    """Oriente un point local puis le déplace au centre du volume."""
    p = _rot(local, *r)
    return (p[0] + centre[0], p[1] + centre[1], p[2] + centre[2])


# -----------------------------------------------------------------------------
# Trait : épaisseur variable, passes multiples (c'est ce qui fait « dessiné main »)
# -----------------------------------------------------------------------------

def _largeur(t, profil):
    """Facteur d'épaisseur (0-1) à la position t (0 = début, 1 = fin) du trait."""
    if profil == "fuseau":   # fin - épais - fin : le trait type d'un dessinateur
        return 0.25 + 0.75 * math.sin(math.pi * t) ** 0.7
    if profil == "pointe":   # appuyé puis relâché : mèche, poil, ligne qui se perd
        return 1.0 - 0.85 * t
    if profil == "attaque":  # posé léger puis appuyé
        return 0.15 + 0.85 * t
    return 1.0               # constant : ligne claire, construction


def trait(lay, pts, size=4.0, col="#1a1a1a", opacity=100.0, profil="fuseau",
          hardness=0.9, passes=1, tremble=0.0, outil="paintbrush", largeurs=None):
    """Trace une polyligne [[x, y], ...] avec une épaisseur qui varie le long du trait.
    largeurs : facteurs d'épaisseur par segment (remplace le profil si donné).
    passes > 1 : redouble le trait avec un léger décalage (tremble, en px) et une opacité
    réduite, comme un crayon qui repasse. Les points doivent être serrés (≤ 15 px).
    Les segments de même épaisseur sont tracés d'un seul geste : pas de « perles »
    d'opacité aux raccords."""
    if len(pts) < 2:
        return
    n = len(pts) - 1
    fac = largeurs or [_largeur((i + 0.5) / n, profil) for i in range(n)]
    # Regroupe les segments consécutifs dont l'épaisseur arrondie (pas de 0,5 px) est la même.
    groupes, cur, w_cur = [], [pts[0]], None
    for i in range(n):
        w = max(0.8, round(size * fac[i] * 2) / 2)
        if w_cur is not None and w != w_cur:
            groupes.append((cur, w_cur))
            cur = [pts[i]]
        cur.append(pts[i + 1])
        w_cur = w
    groupes.append((cur, w_cur))
    for k in range(passes):
        dx = random.uniform(-tremble, tremble) if k else 0.0
        dy = random.uniform(-tremble, tremble) if k else 0.0
        Gimp.context_push()
        Gimp.context_set_foreground(color(col))
        Gimp.context_set_brush_hardness(hardness)
        Gimp.context_set_opacity(opacity if k == 0 else opacity * 0.45)
        for g, w in groupes:
            Gimp.context_set_brush_size(w)
            flat = [v for p in g for v in (p[0] + dx, p[1] + dy)]
            if outil == "pencil":
                Gimp.pencil(lay, flat)
            else:
                Gimp.paintbrush_default(lay, flat)
        Gimp.context_pop()


def dessiner(lay, vol, size=3.0, col="#7d95b5", opacity=100.0, caches=True, papier=None):
    """Dessine un volume : (papier) remplit sa silhouette pour cacher ce qui est derrière,
    puis trace la silhouette, puis les contours croisés visibles, puis (caches) les lignes
    cachées en très léger. size est modulé par la profondeur : plus près = plus épais."""
    s = size * vol["k"]
    if papier:
        img = lay.get_image()
        flat = [v for p in vol["silhouette"] for v in p]
        Gimp.context_push()
        Gimp.context_set_foreground(color(papier))
        img.select_polygon(Gimp.ChannelOps.REPLACE, flat)
        lay.edit_fill(Gimp.FillType.FOREGROUND)
        Gimp.Selection.none(img)
        Gimp.context_pop()
    sil = vol["silhouette"] + [vol["silhouette"][0]]
    trait(lay, sil, size=s * 1.4, col=col, opacity=opacity, profil="constant")
    for pts, vis in vol["lignes"]:
        if vis:
            trait(lay, pts, size=s * 0.8, col=col, opacity=opacity * 0.8, profil="fuseau")
        elif caches:
            trait(lay, pts, size=s * 0.5, col=col, opacity=opacity * 0.25, profil="constant")


def scene(lay, volumes, size=3.0, col="#7d95b5", papier="#f6f1e8", caches=False):
    """Dessine plusieurs volumes du plus lointain au plus proche : chaque volume recouvre
    ceux qu'il a derrière lui (papier), d'où des recouvrements justes, base de la profondeur."""
    for vol in sorted(volumes, key=lambda v: -v["z"]):
        dessiner(lay, vol, size=size, col=col, caches=caches, papier=papier)
    Gimp.displays_flush()


def sol(lay, y=0.0, etendue=600.0, pas=150.0, col="#b9c7d8", size=2.0):
    """Grille de sol en perspective sous le sujet : pose immédiatement l'espace et l'horizon."""
    n = int(etendue // pas)
    for i in range(-n, n + 1):
        a = projeter((i * pas, y, -etendue))
        b = projeter((i * pas, y, etendue))
        trait(lay, _subdiv(a, b), size=size, col=col, opacity=60, profil="constant")
        a = projeter((-etendue, y, i * pas))
        b = projeter((etendue, y, i * pas))
        trait(lay, _subdiv(a, b), size=size, col=col, opacity=60, profil="constant")


def courbe3d(points3d, n=24):
    """Courbe lisse passant par des points 3D (Catmull-Rom), projetée en 2D : ligne d'action,
    colonne vertébrale, contour d'oreille ou de mèche placé dans l'espace."""
    P = [points3d[0]] + list(points3d) + [points3d[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(projeter(tuple(0.5 * (2 * p1[d] + (-p0[d] + p2[d]) * t +
                                              (2 * p0[d] - 5 * p1[d] + 4 * p2[d] - p3[d]) * t2 +
                                              (-p0[d] + 3 * p1[d] - 3 * p2[d] + p3[d]) * t3)
                                       for d in range(3))))
    out.append(projeter(points3d[-1]))
    return out


def _subdiv(a, b, pas=12.0):
    """Découpe un segment en petits pas (le trait à épaisseur variable en a besoin)."""
    n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) // pas))
    return [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]


# -----------------------------------------------------------------------------
# Cadrage : faire tenir le sujet dans l'image sans tâtonner
# -----------------------------------------------------------------------------

def cadrer(construire, largeur, hauteur, occupation=0.78, decalage=(0.0, 0.0)):
    """construire : fonction sans argument qui renvoie la liste des volumes (elle est
    rappelée après réglage). Ajuste zoom et centre de la caméra pour que le sujet occupe
    `occupation` de la hauteur (ou de la largeur si plus contraignant), centré, puis
    décalé de `decalage` (fraction de l'image, ex. (-0.06, 0) : un peu à gauche pour
    laisser de l'air devant le regard). Renvoie les volumes recalculés."""
    vols = construire()
    xs = [p[0] for v in vols for p in v["silhouette"]]
    ys = [p[1] for v in vols for p in v["silhouette"]]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = min(occupation * hauteur / h, occupation * largeur / w)
    mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    # Zoom autour du centre de projection, puis déplacement du centre du sujet.
    CAM["f"] *= s
    CAM["cx"] += (largeur * (0.5 + decalage[0])) - (CAM["cx"] + (mx - CAM["cx"]) * s)
    CAM["cy"] += (hauteur * (0.5 + decalage[1])) - (CAM["cy"] + (my - CAM["cy"]) * s)
    return construire()


# -----------------------------------------------------------------------------
# Croquis automatique : le contour réel du sujet, à partir des volumes
# -----------------------------------------------------------------------------

def _dedans(p, poly):
    """Le point p est-il à l'intérieur du polygone convexe poly ?"""
    signe = 0
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        c = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if abs(c) > 1e-6:
            s = 1 if c > 0 else -1
            if signe == 0:
                signe = s
            elif s != signe:
                return False
    return True


def _reechantillonner(poly, pas):
    """Points régulièrement espacés de `pas` px le long d'un polygone fermé."""
    out = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        out += _subdiv(a, b, pas)[:-1]
    return out


def contours_visibles(volumes, pas=6.0):
    """Pour chaque volume, les morceaux de sa silhouette qui ne sont cachés par aucun volume
    plus proche. Chaque morceau est un dict : pts, exterieur (True = bord de la silhouette
    globale du sujet ; False = ligne de recouvrement à l'intérieur), nom, centre (2D)."""
    vols = sorted(volumes, key=lambda v: v["z"])  # du plus proche au plus lointain
    morceaux = []
    for i, v in enumerate(vols):
        sil = _reechantillonner(v["silhouette"], pas)
        devant = [w["silhouette"] for w in vols[:i]]
        autres = [w["silhouette"] for j, w in enumerate(vols) if j != i]
        etats = []
        for p in sil:
            if any(_dedans(p, s) for s in devant):
                etats.append(None)  # caché par un volume plus proche
            else:
                etats.append(not any(_dedans(p, s) for s in autres))
        # Fait démarrer la boucle sur un changement d'état pour ne pas couper un morceau en deux.
        dep = next((k for k in range(len(etats)) if etats[k] != etats[k - 1]), 0)
        sil, etats = sil[dep:] + sil[:dep], etats[dep:] + etats[:dep]
        cx = sum(p[0] for p in v["silhouette"]) / len(v["silhouette"])
        cy = sum(p[1] for p in v["silhouette"]) / len(v["silhouette"])
        # Découpe en morceaux de même état ; chaque morceau reprend le premier point du
        # suivant pour qu'il n'y ait pas de trou dans le trait.
        runs, cur = [], [sil[0]]
        for k in range(1, len(sil)):
            if etats[k] != etats[k - 1]:
                runs.append((cur + [sil[k]], etats[k - 1]))
                cur = []
            cur.append(sil[k])
        runs.append((cur + [sil[0]], etats[-1]))  # le dernier morceau referme la boucle
        for pts, e in runs:
            if e is not None and len(pts) > 2:
                morceaux.append({"pts": pts, "exterieur": e, "nom": v["nom"], "centre": (cx, cy)})
    return morceaux


def _normale(a, b, centre):
    """Normale unitaire du segment ab, orientée vers l'extérieur du volume (loin du centre)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    l = math.hypot(dx, dy) or 1.0
    nx, ny = dy / l, -dx / l
    mx, my = (a[0] + b[0]) / 2 - centre[0], (a[1] + b[1]) / 2 - centre[1]
    if nx * mx + ny * my < 0:
        nx, ny = -nx, -ny
    return nx, ny


def croquis_auto(lay, volumes, size=6.0, col="#3a3330", lumiere=(-1.0, -1.0), opacity=90.0,
                 perdu=0.8, fourrure=(), poil=22.0, passes=2, tremble=1.5):
    """Trace le croquis de base du sujet à partir de ses volumes :
    - bord extérieur épais côté ombre, fin côté lumière, et interrompu (« ligne perdue »)
      là où il fait face à la lumière au-delà du seuil `perdu` (0-1 ; 1 = jamais perdu) ;
    - lignes de recouvrement intérieures plus fines, effilées aux deux bouts ;
    - fourrure : pour les volumes dont le nom est dans `fourrure`, des touffes effilées
      sur le bord extérieur, orientées vers l'extérieur et un peu vers l'arrière.
    lumiere = direction d'où vient la lumière dans l'image ((-1, -1) = haut-gauche)."""
    lx, ly = lumiere
    ll = math.hypot(lx, ly) or 1.0
    lx, ly = lx / ll, ly / ll
    for m in contours_visibles(volumes):
        pts, c = m["pts"], m["centre"]
        n = len(pts) - 1
        if n < 2:
            continue
        k = size * (1.0 if m["exterieur"] else 0.6)
        fac, garde = [], []
        for i in range(n):
            nx, ny = _normale(pts[i], pts[i + 1], c)
            face = nx * lx + ny * ly  # 1 = face à la lumière, -1 = dos à la lumière
            t = (i + 0.5) / n
            bout = min(1.0, 4 * t, 4 * (1 - t)) if not m["exterieur"] else 1.0
            fac.append(max(0.25, (1.0 - 0.5 * face) * (0.35 + 0.65 * bout)))
            garde.append(not (m["exterieur"] and face > perdu))
        # Trace chaque portion non perdue comme un geste séparé.
        debut = None
        for i in range(n + 1):
            ok = i < n and garde[i]
            if ok and debut is None:
                debut = i
            if not ok and debut is not None:
                trait(lay, pts[debut:i + 1], size=k, col=col, opacity=opacity,
                      largeurs=fac[debut:i], passes=passes, tremble=tremble)
                debut = None
        if m["exterieur"] and m["nom"] in fourrure:
            for t in bord_fourrure(pts, centre=c, taille=poil):
                trait(lay, t, size=size * 0.6, col=col, opacity=opacity * 0.9, profil="fuseau")
    Gimp.displays_flush()


# -----------------------------------------------------------------------------
# Dessin libre : courbes 2D et fourrure
# -----------------------------------------------------------------------------

def courbe2d(points, n=10):
    """Courbe lisse (Catmull-Rom) qui passe exactement par les points [[x, y], ...] donnés.
    C'est l'outil du trait final : on pose 4 à 10 points lus sur la construction, la courbe
    les relie sans angle. n = points intermédiaires par intervalle."""
    P = [points[0]] + [list(p) for p in points] + [points[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append([0.5 * (2 * p1[d] + (-p0[d] + p2[d]) * t + (2 * p0[d] - 5 * p1[d] + 4 * p2[d] - p3[d]) * t2 +
                               (-p0[d] + 3 * p1[d] - 3 * p2[d] + p3[d]) * t3) for d in range(2)])
    out.append(list(points[-1]))
    return out


def bord_fourrure(pts, centre=None, cote=1, taille=20.0, largeur=26.0, penche=0.5, graine=None):
    """Transforme un bord lisse en bord de fourrure : une suite de touffes en pointe
    (zigzag irrégulier) qui REMPLACE le contour à cet endroit. Renvoie une liste de
    petites polylignes, une par touffe, à tracer chacune avec trait(..., profil="fuseau").
    centre : si donné, les pointes partent vers l'extérieur (loin de ce point) ;
             sinon cote=1 : à gauche du sens de tracé à l'écran, -1 : à droite.
    taille : longueur des pointes ; largeur : base d'une touffe le long du bord ;
    penche : les pointes se couchent dans le sens du tracé (0 = droites, 1 = très couchées)."""
    if graine is not None:
        random.seed(graine)
    # Rééchantillonne le bord à pas fixe pour des touffes régulières.
    lisse = []
    for i in range(len(pts) - 1):
        lisse += _subdiv(pts[i], pts[i + 1], 4.0)[:-1]
    lisse.append(pts[-1])
    touffes_ = []
    i = 0
    while i < len(lisse) - 2:
        # Largeur et longueur très variables d'une touffe à l'autre : une fourrure régulière
        # ressemble à une lame de scie.
        pas = max(2, int(largeur * random.uniform(0.55, 1.6) / 4.0))
        j = min(len(lisse) - 1, i + pas)
        a, b = lisse[i], lisse[j]
        if random.random() < 0.12 and j - i > 3:
            touffes_.append(lisse[i:j + 1])  # court passage lisse : le poil est couché ici
            i = j
            continue
        tx, ty = b[0] - a[0], b[1] - a[1]
        tl = math.hypot(tx, ty) or 1.0
        tx, ty = tx / tl, ty / tl
        if centre is not None:
            nx, ny = _normale(a, b, centre)
        else:
            nx, ny = ty * cote, -tx * cote
        m = lisse[(i + j) // 2]
        L = taille * random.uniform(0.35, 1.25)
        p = penche * random.uniform(0.6, 1.5)
        pointe = [m[0] + nx * L + tx * L * p, m[1] + ny * L + ty * L * p]
        # Aller bombé vers l'extérieur, retour creusé : la touffe a la forme d'une flamme.
        aller = courbe2d([a, [(a[0] + pointe[0]) / 2 + nx * L * 0.18, (a[1] + pointe[1]) / 2 + ny * L * 0.18], pointe], n=5)
        if random.random() < 0.25:
            # Pointe double : une seconde petite mèche sur le retour.
            q = [(pointe[0] + b[0]) / 2 + nx * L * 0.35, (pointe[1] + b[1]) / 2 + ny * L * 0.35]
            creux = [(pointe[0] + q[0]) / 2 - nx * L * 0.2, (pointe[1] + q[1]) / 2 - ny * L * 0.2]
            retour = courbe2d([pointe, creux, q, b], n=4)
        else:
            retour = courbe2d([pointe, [(pointe[0] + b[0]) / 2 - nx * L * 0.12, (pointe[1] + b[1]) / 2 - ny * L * 0.12], b], n=5)
        touffes_.append(aller + retour[1:])
        i = j
    return touffes_


# -----------------------------------------------------------------------------
# Yeux : posés sur la surface de la tête, donc raccourcis correctement en perspective
# -----------------------------------------------------------------------------

def _selection_polygone(lay, pts, mode="replace"):
    """Sélection polygonale (replace / intersect / add) à partir de points [[x, y], ...]."""
    op = {"replace": Gimp.ChannelOps.REPLACE, "intersect": Gimp.ChannelOps.INTERSECT,
          "add": Gimp.ChannelOps.ADD}[mode]
    lay.get_image().select_polygon(op, [v for p in pts for v in p])


def _remplir(lay, col, opacity=100.0):
    """Remplit la sélection courante du calque avec une couleur."""
    Gimp.context_push()
    Gimp.context_set_foreground(color(col))
    Gimp.context_set_opacity(opacity)
    lay.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.context_pop()


def _cercle(c, r, n=28):
    """Polygone de n points approchant un cercle de centre c et de rayon r."""
    return [[c[0] + r * math.cos(2 * math.pi * k / n), c[1] + r * math.sin(2 * math.pi * k / n)] for k in range(n)]


def oeil(lay, tete, lat=8.0, lon=-25.0, taille=26.0, ouverture=0.42, obliquite=0.3,
         regard=(0.0, 0.0), pupille="ronde", iris="#9a8670", col="#2a2320", paupiere=7.0,
         larme=False, pics=(-1 / 3, 1 / 3), ferme=False):
    """Dessine un œil complet posé sur le volume `tete` (un ellipsoïde renvoyé par
    ellipsoide(), retrouvé par exemple avec par_nom(V, "crane")). Le coin interne est placé
    automatiquement du côté du nez.
    lat, lon : position sur la tête en degrés (lat 0 = équateur ; lon 0 = milieu du visage,
               négatif = côté gauche du sujet). Les deux yeux : même lat, lon opposées,
               en général ±22 à ±30 (au-delà, l'œil éloigné passe derrière le profil).
    taille   : demi-largeur de l'œil en px (avant perspective).
    ouverture: hauteur de l'ouverture / largeur (0,25 paupières lourdes ; 0,6 grands yeux ronds).
    obliquite: le coin externe remonte (0 = horizontal ; 0,3-0,5 = renard, chat, regard malin).
    regard   : décalage de l'iris (-1..1, -1..1) ; donne la même valeur aux deux yeux.
    pupille  : "ronde" ou "fente" (verticale : chats, renards).
    paupiere : épaisseur max du trait de paupière supérieure.
    larme    : trace la ligne de larme qui descend du coin interne (renard, guépard).
    pics     : position (de -1 = coin interne à +1 = coin externe) du sommet de la paupière
               supérieure et du point bas de la paupière inférieure. Le défaut (-1/3, +1/3)
               suit l'anatomie humaine : sommet au tiers côté nez, point bas aux deux tiers ;
               c'est ce décalage qui évite l'œil en « citron » symétrique.
    ferme    : œil fermé (sommeil, contentement) : la paupière supérieure descend sur la
               paupière inférieure ; on ne dessine qu'un arc tourné vers le bas, épais au milieu,
               avec une petite pointe au coin externe."""
    a, b, c = tete["rayons"]
    r = [math.radians(v) for v in tete["rot"]]
    la, lo = math.radians(lat), math.radians(lon)

    def surf(phi, th):
        # Point de l'ellipsoïde ; th mesuré depuis la face avant (z négatif = vers la caméra).
        return (a * math.cos(phi) * math.sin(th), b * math.sin(phi), -c * math.cos(phi) * math.cos(th))

    P = surf(la, lo)
    # Un visage n'est pas une boule : les yeux sont sur un plan de face à peine courbé.
    # On oriente donc le plan de l'œil à mi-chemin entre la face avant de la tête et la
    # surface de la sphère (coefficient 0,45) : l'œil éloigné se raccourcit sans basculer.
    k = 0.45
    U = [math.cos(lo * k), 0.0, math.sin(lo * k)]    # horizontale de l'œil
    Vv = [0.0, math.cos(la * k), math.sin(la * k)]    # verticale de l'œil

    def ecran(s, t):
        """Point (s, t) du plan de l'œil (unités = taille) -> pixels image."""
        loc = tuple(P[k] + U[k] * s * taille + Vv[k] * t * taille for k in range(3))
        return projeter(_place(loc, tete["centre"], r))

    # Coin interne côté nez : le nez est vers lon = 0, donc du côté opposé au signe de lon.
    sens = -1.0 if lon < 0 else 1.0           # s = sens pointe vers l'extérieur de la tête
    def pt(s, t):
        return ecran(s * sens, t)

    def bosse(s, pic):
        """Courbe 0 aux coins, 1 au sommet placé en s = pic (asymétrique)."""
        u = (s - pic) / ((pic + 1) if s < pic else (1 - pic))
        return max(0.0, 1 - u * u) ** 0.75

    ns = 16
    sup, inf = [], []
    for k in range(ns + 1):
        s = -1 + 2 * k / ns                     # -1 = coin interne, +1 = coin externe
        base = obliquite * (s + 1) / 2 * 0.9   # le coin externe remonte
        sup.append(pt(s, base + 1.15 * ouverture * bosse(s, pics[0])))
        inf.append(pt(s, base - 0.6 * ouverture * bosse(s, pics[1])))
    ouvert = sup + inf[::-1]

    if ferme:
        # La paupière fermée suit le bas de l'œil : un arc qui creuse vers le bas.
        arc = [pt(-1 + 2 * k / ns, obliquite * (k / ns) * 0.9 - 0.45 * ouverture * bosse(-1 + 2 * k / ns, 0.1))
               for k in range(ns + 1)]
        trait(lay, arc, size=paupiere, col=col, profil="fuseau")
        trait(lay, courbe2d([arc[-2], arc[-1], pt(1.18, obliquite * 0.9 - 0.12)], 4),
              size=paupiere * 0.6, col=col, profil="pointe")
        return

    # Iris et pupille, découpés par l'ouverture de l'œil (sélection intersectée).
    ci = pt(0.05 + 0.35 * regard[0], obliquite * 0.5 + 0.12 * ouverture + 0.25 * ouverture * regard[1])
    larg = math.hypot(sup[-1][0] - sup[0][0], sup[-1][1] - sup[0][1]) / 2
    ri = larg * 0.48
    _selection_polygone(lay, ouvert)
    _remplir(lay, "#fbf8f2")                                    # blanc de l'œil
    _selection_polygone(lay, ouvert)
    _selection_polygone(lay, _cercle(ci, ri), "intersect")
    _remplir(lay, iris)
    _selection_polygone(lay, ouvert)
    if pupille == "fente":
        # Fente verticale en forme de lentille : arc droit de haut en bas, puis arc gauche.
        fente = [[ci[0] + ri * 0.22 * math.sin(math.pi * k / 10), ci[1] - ri * 0.95 * math.cos(math.pi * k / 10)]
                 for k in range(11)]
        fente += [[ci[0] - ri * 0.22 * math.sin(math.pi * k / 10), ci[1] - ri * 0.95 * math.cos(math.pi * k / 10)]
                  for k in range(9, 0, -1)]
        _selection_polygone(lay, fente, "intersect")
    else:
        _selection_polygone(lay, _cercle(ci, ri * 0.5), "intersect")
    _remplir(lay, col)
    Gimp.Selection.none(lay.get_image())
    # Contour de l'iris (léger), reflet (non découpé : il accroche la lumière).
    trait(lay, _cercle(ci, ri) + [_cercle(ci, ri)[0]], size=1.5, col=col, opacity=45, profil="constant")
    reflet = [ci[0] - ri * 0.35, ci[1] - ri * 0.4]
    _selection_polygone(lay, _cercle(reflet, max(1.5, ri * 0.22), 12))
    _remplir(lay, "#ffffff")
    Gimp.Selection.none(lay.get_image())
    # Paupière supérieure : fine au coin interne, épaisse vers l'extérieur, petite virgule finale.
    n = len(sup) - 1
    trait(lay, sup, size=paupiere, col=col, largeurs=[0.35 + 0.65 * (i / n) ** 0.7 for i in range(n)])
    # Raccourci de l'œil : largeur à l'écran / hauteur à l'écran comparée à la forme à plat.
    haut = math.hypot(pt(0, 1)[0] - pt(0, -1)[0], pt(0, 1)[1] - pt(0, -1)[1]) / 2
    raccourci = min(1.0, larg / max(1e-6, haut))
    if raccourci > 0.75:  # œil vu presque de face : virgule au coin externe
        fin = pt(1.22, obliquite * 0.9 + 0.18 * ouverture)
        trait(lay, courbe2d([sup[-2], sup[-1], fin], 4), size=paupiere * 0.8, col=col, profil="pointe")
    # Paupière inférieure : partielle, côté externe seulement, fine.
    k0 = int(ns * 0.35)
    trait(lay, inf[k0:], size=paupiere * 0.35, col=col, opacity=75, profil="attaque")
    # Coin interne : petite pointe vers le nez ; ligne de larme facultative.
    trait(lay, [sup[0], pt(-1.12, -0.04)], size=paupiere * 0.4, col=col, profil="pointe")
    if larme:
        trait(lay, courbe2d([pt(-1.08, -0.08), pt(-1.16, -0.4), pt(-1.12, -0.75)], 6),
              size=paupiere * 0.45, col=col, opacity=80, profil="pointe")


# -----------------------------------------------------------------------------
# Membres : pattes, bras, jambes articulés, et pieds avec doigts
# -----------------------------------------------------------------------------

def patte(lay, articulations, largeurs, renflements=None, dos=1, size=6.0, col="#2a2320",
          lumiere=-1, opacity=92.0, passes=2, papier=None):
    """Dessine un membre à partir de ses articulations (du haut vers le bas, en px image :
    épaule, coude, poignet, base du pied) et de sa largeur à chaque articulation.
    renflements : bosse ajoutée côté `dos` à chaque articulation (muscle, coude, talon) ;
                  ex. [0, 10, 0, 0] pour un coude marqué.
    dos         : +1 ou -1, côté (gauche/droite du sens haut->bas à l'écran) qui reçoit les renflements.
    lumiere     : côté éclairé (+1 / -1, même convention) : ce bord est plus fin.
    Les deux bords sont des courbes lisses ; le membre s'affine aux articulations osseuses
    (poignet, cheville) si tu lui donnes une largeur plus petite à cet endroit.
    papier : couleur du fond ; si donnée, le membre cache les traits déjà tracés derrière lui
             (dessine donc les membres proches en dernier).
    Renvoie {"avant": …, "arriere": …} : les points bas des deux bords, à passer à
    pied(attache=…) pour que le pied se raccorde exactement (l'arrière = le côté `dos`)."""
    ren = renflements or [0] * len(articulations)
    axe = courbe2d(articulations, 10)
    # Largeur et renflement interpolés le long de l'axe (10 pas par intervalle).
    def interp(vals, i):
        seg = min(len(vals) - 2, i // 10)
        t = (i - seg * 10) / 10
        t = t * t * (3 - 2 * t)                      # transition douce entre articulations
        return vals[seg] * (1 - t) + vals[seg + 1] * t
    gauche, droite = [], []
    for i, p in enumerate(axe):
        q = axe[min(i + 1, len(axe) - 1)] if i < len(axe) - 1 else p
        o = axe[i - 1] if i > 0 else p
        tx, ty = q[0] - o[0], q[1] - o[1]
        l = math.hypot(tx, ty) or 1.0
        nx, ny = ty / l, -tx / l                     # normale « gauche » à l'écran
        w = interp(largeurs, i) / 2
        bg = interp(ren, i) if dos == 1 else 0
        bd = interp(ren, i) if dos == -1 else 0
        gauche.append([p[0] + nx * (w + bg), p[1] + ny * (w + bg)])
        droite.append([p[0] - nx * (w + bd), p[1] - ny * (w + bd)])
    if papier:
        _selection_polygone(lay, gauche + droite[::-1])
        _remplir(lay, papier)
        Gimp.Selection.none(lay.get_image())
    for bord, cote in ((gauche, 1), (droite, -1)):
        k = 0.65 if cote == lumiere else 1.0
        trait(lay, bord, size=size * k, col=col, opacity=opacity, profil="fuseau", passes=passes, tremble=1.2)
    arriere, avant = (gauche[-1], droite[-1]) if dos == 1 else (droite[-1], gauche[-1])
    return {"avant": avant, "arriere": arriere}


def _finir_pied(pts, larg, sens):
    """Termine un membre sur le dessus du pied au lieu du sol, et calcule un pied
    proportionné : largeur ≈ 1,7 × la largeur du métacarpe, longueur ≈ 1,3 × sa largeur."""
    sol = pts[-1]
    lp = larg[-2] * 1.7                                # largeur du pied
    pts[-1] = [sol[0], sol[1] - lp * 0.5]              # le trait de la patte se pose sur le pied
    talon = [sol[0] - sens * lp * 0.35, sol[1]]
    orteils = [talon[0] + sens * lp * 1.3, sol[1]]
    return {"talon": talon, "orteils": orteils, "largeur_pied": lp}


def membre_quadrupede(depart, hauteur, sens=-1, membre="avant", pose="debout", largeur=None):
    """Place les articulations d'une patte de quadrupède digitigrade (chien, renard, loup,
    chat) aux angles anatomiques des standards canins (voir references/anatomie-animaux.md) :
      avant  : omoplate ~45° vers l'avant-bas, bras à ~90° de l'omoplate (vers l'arrière-bas),
               avant-bras vertical, métacarpe (« pastern ») légèrement incliné vers l'avant (15°),
               pied ;
      arrière debout : bassin incliné de 30°, fémur à ~90° du bassin (vers l'avant-bas), angle
               du grasset (genou) ~115°, métatarse vertical sous le jarret (talon), pied ;
      arrière assis  : cuisse presque horizontale vers l'avant, jambe qui redescend vers
               l'arrière, jarret posé au sol, métatarse couché au sol vers l'avant.
    depart  : [x, y] image du haut du membre (garrot pour l'avant, hanche pour l'arrière).
    hauteur : distance verticale en px du départ jusqu'au sol.
    sens    : direction vers laquelle regarde l'animal à l'écran (-1 = gauche, +1 = droite).
    Renvoie un dict : 'articulations' (du haut au pied), 'largeurs' proposées, 'visible'
    (indice de la première articulation visible hors du corps : coude ou grasset), 'talon'
    et 'orteils' pour pied()."""
    x0, y0 = depart
    deg = math.radians

    def pas(p, angle_deg, L):
        """Avance de L dans la direction angle (0° = vers l'avant de l'animal, 90° = vers le bas)."""
        a = deg(angle_deg)
        return [p[0] + sens * L * math.cos(a), p[1] + L * math.sin(a)]

    if membre == "avant":
        # Proportions des segments (omoplate = bras, standard ; avant-bras et métacarpe : choix
        # de dessin courant) ; on résout la longueur pour toucher le sol à `hauteur`.
        k = [1.0, 1.0, 1.15, 0.38]
        angles = [45, 135, 90, 75]                    # omoplate, bras, avant-bras, métacarpe
        dy = sum(ki * math.sin(deg(a)) for ki, a in zip(k, angles))
        L = hauteur / dy
        pts = [[x0, y0]]
        for ki, a in zip(k, angles):
            pts.append(pas(pts[-1], a, ki * L))
        w = largeur or L * 0.42
        larg = [w * 1.6, w * 1.25, w * 0.95, w * 0.62, w * 0.7]   # le poignet est le plus fin
        return {"articulations": pts, "largeurs": larg, "visible": 2, **_finir_pied(pts, larg, sens)}
    if pose == "debout":
        k = [1.0, 1.1, 0.6]                           # fémur, jambe (≥ fémur), métatarse
        # Convention d'angle : 0° = vers l'avant de l'animal, 90° = vers le bas, 180° = vers l'arrière.
        a_femur = 60                                  # bassin à 30° + fémur à 90° du bassin
        # Au grasset, l'angle entre « vers la hanche » (60 + 180 = 240°) et « vers le jarret »
        # vaut 115° : la jambe part donc à 240 - 115 = 125°, vers l'arrière-bas.
        a_jambe = (a_femur + 180) - 115
        angles = [a_femur, a_jambe, 90]               # métatarse vertical sous le jarret
        dy = sum(ki * math.sin(deg(a)) for ki, a in zip(k, angles))
        L = hauteur / dy
        pts = [[x0, y0]]
        for ki, a in zip(k, angles):
            pts.append(pas(pts[-1], a, ki * L))
        w = largeur or L * 0.5
        larg = [w * 2.0, w * 1.15, w * 0.55, w * 0.62]
        return {"articulations": pts, "largeurs": larg, "visible": 1, **_finir_pied(pts, larg, sens)}
    # Arrière assis : la cuisse forme une grosse masse ronde ; le jarret touche le sol.
    L = hauteur / 1.0
    hanche = [x0, y0]
    grasset = pas(hanche, 20, L * 0.75)               # genou en avant, un peu plus bas
    jarret = [hanche[0] - sens * L * 0.15, y0 + hauteur]  # talon au sol, sous la hanche
    orteils = pas(jarret, 0, L * 0.75)                 # métatarse couché au sol vers l'avant
    w = largeur or L * 0.35
    return {"articulations": [hanche, grasset, jarret, orteils], "largeurs": [w * 2.4, w * 1.6, w * 0.6, w * 0.7],
            "visible": 0, "talon": jarret, "orteils": orteils, "largeur_pied": w * 1.2}


def pied(lay, talon, orteils, largeur, doigts=3, col="#2a2320", size=6.0, opacity=92.0,
         attache=None, griffes=False, papier=None):
    """Pied ou patte posé au sol, dessiné en « moufle » comme dans les méthodes de dessin
    animalier : un bloc arrondi dont le devant prolonge le devant de la patte, dont l'arrière
    se termine par le talon, avec 2-3 encoches qui indiquent les doigts.
    talon, orteils : [x, y] au sol, de l'arrière du pied vers le bout des doigts.
    largeur  : sert d'échelle (hauteur du pied ≈ 0,55 × largeur).
    doigts   : encoches visibles (3 de côté ou de 3/4 ; 4 de face).
    attache  : le dict renvoyé par patte() ({"avant", "arriere"}) ; le pied se raccorde alors
               exactement aux deux bords de la patte.
    papier   : couleur du fond ; le pied cache ce qui est derrière lui."""
    dx, dy = orteils[0] - talon[0], orteils[1] - talon[1]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L                       # vers les doigts
    h = largeur * 0.55

    def pt(a, c):
        """a : le long du pied depuis le talon (px) ; c : hauteur au-dessus du sol (px)."""
        return [talon[0] + ux * a, talon[1] + uy * a - c]

    if attache:
        avant, arriere = attache["avant"], attache["arriere"]
    else:
        avant, arriere = pt(L * 0.45, h * 1.1), pt(L * 0.05, h * 1.1)
    # Contour de la moufle : devant de la patte -> dessus des doigts -> bout arrondi -> sol -> talon -> arrière de la patte.
    dessus = courbe2d([avant, pt(L * 0.7, h * 0.85), pt(L * 0.95, h * 0.55), pt(L * 1.02, h * 0.2), pt(L * 0.93, 0)], 6)
    semelle = courbe2d([pt(L * 0.93, 0), pt(L * 0.5, -1), pt(L * 0.08, 0)], 6)
    talon_c = courbe2d([pt(L * 0.08, 0), pt(-L * 0.04, h * 0.25), pt(L * 0.0, h * 0.6), arriere], 6)
    if papier:
        _selection_polygone(lay, dessus + semelle + talon_c)
        _remplir(lay, papier)
        Gimp.Selection.none(lay.get_image())
    trait(lay, dessus, size=size * 0.85, col=col, opacity=opacity, profil="fuseau", passes=2, tremble=1.0)
    trait(lay, semelle, size=size * 1.15, col=col, opacity=opacity * 0.9, profil="fuseau")   # contact au sol : épais
    trait(lay, talon_c, size=size * 0.9, col=col, opacity=opacity, profil="fuseau")
    # Encoches des doigts : petites courbes qui descendent du dessus vers l'avant.
    for k in range(1, doigts + 1):
        a = L * (0.52 + 0.42 * k / (doigts + 1))
        haut = pt(a, h * (0.85 - 0.35 * k / (doigts + 1)))
        bas = pt(a + L * 0.06, h * (0.35 - 0.2 * k / (doigts + 1)))
        trait(lay, courbe2d([haut, [(haut[0] + bas[0]) / 2 + ux * L * 0.03, (haut[1] + bas[1]) / 2], bas], 4),
              size=size * 0.5, col=col, opacity=opacity * 0.85, profil="pointe")
        if griffes:
            trait(lay, [bas, pt(a + L * 0.14, 0)], size=size * 0.4, col=col, profil="pointe")


result = ("volumes3d chargé : camera, cadrer, ellipsoide, boite, cylindre, par_nom, scene, sol, "
          "projeter, courbe3d, courbe2d, trait, bord_fourrure, contours_visibles, croquis_auto, "
          "oeil, membre_quadrupede, patte, pied")
