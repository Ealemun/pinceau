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
            "lignes": lignes, "z": zc, "k": CAM["f"] / zc, "surf3d": silhouette3d}


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


def _lisser(pts, pas=2.5):
    """Rend une polyligne lisse et régulière : deux passes de Chaikin (coins arrondis), puis
    rééchantillonnage tous les `pas` px. C'est ce qui supprime l'aspect saccadé d'une courbe
    donnée en quelques points."""
    P = [list(p) for p in pts]
    for _ in range(2):
        if len(P) < 3:
            break
        Q = [P[0]]
        for a, b in zip(P[:-1], P[1:]):
            Q.append([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]])
            Q.append([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]])
        Q.append(P[-1])
        P = Q
    # Rééchantillonnage à pas constant le long de la courbe.
    out, reste = [P[0]], 0.0
    for a, b in zip(P[:-1], P[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        d = pas - reste
        while d <= L:
            t = d / L
            out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
            d += pas
        reste = L - (d - pas)
    if math.hypot(out[-1][0] - P[-1][0], out[-1][1] - P[-1][1]) > 0.3:
        out.append(P[-1])
    return out


def _ruban(pts, largeurs):
    """Contour fermé d'un trait d'épaisseur variable : bord gauche, embout rond, bord droit
    à rebours, embout rond. largeurs : diamètre en chaque point."""
    n = len(pts)
    gauche, droite = [], []
    for i in range(n):
        a, b = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        l = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / l, tx / l
        r = largeurs[i] / 2.0
        gauche.append([pts[i][0] + nx * r, pts[i][1] + ny * r])
        droite.append([pts[i][0] - nx * r, pts[i][1] - ny * r])

    def embout(c, depart, rayon, sens):
        # Demi-cercle autour de c, de l'angle `depart` sur 180° (sens = ±1).
        return [[c[0] + rayon * math.cos(depart + sens * math.pi * k / 6),
                 c[1] + rayon * math.sin(depart + sens * math.pi * k / 6)] for k in range(1, 6)]

    def angle(p, c):
        return math.atan2(p[1] - c[1], p[0] - c[0])
    fin = embout(pts[-1], angle(gauche[-1], pts[-1]), largeurs[-1] / 2.0, -1)
    deb = embout(pts[0], angle(droite[0], pts[0]), largeurs[0] / 2.0, -1)
    return gauche + fin + droite[::-1] + deb


def _selection_vide(lay):
    """Vrai s'il n'y a pas de sélection active (le trait peut alors poser la sienne)."""
    try:
        return bool(Gimp.Selection.is_empty(lay.get_image()))
    except Exception:
        return True


def trait(lay, pts, size=4.0, col="#1a1a1a", opacity=100.0, profil="fuseau",
          hardness=0.9, passes=1, tremble=0.0, outil="paintbrush", largeurs=None):
    """Trace une polyligne [[x, y], ...] avec une épaisseur qui varie le long du trait.
    largeurs : facteurs d'épaisseur par segment (remplace le profil si donné).
    passes > 1 : redouble le trait avec un léger décalage (tremble, en px) et une opacité
    réduite, comme un crayon qui repasse.
    Rendu : la courbe est lissée puis tracée comme UN SEUL ruban rempli dont la largeur varie
    continûment (pas de raccords, pas de perles : un trait fluide, comme une vraie pression).
    Pour un trait doux (hardness < 0,6), au crayon (outil="pencil") ou quand une sélection est
    déjà active (elle sert à rogner les traits), on revient à la brosse de GIMP."""
    if len(pts) < 2:
        return
    n = len(pts) - 1
    fac = largeurs or [_largeur((i + 0.5) / n, profil) for i in range(n)]
    ruban = outil == "paintbrush" and hardness >= 0.6 and _selection_vide(lay)
    if ruban:
        lisse = _lisser(pts)
        # Largeur en chaque point lissé : interpolée le long de la longueur du trait d'origine.
        cum = [0.0]
        for a, b in zip(pts[:-1], pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        cl = [0.0]
        for a, b in zip(lisse[:-1], lisse[1:]):
            cl.append(cl[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        tot, totl = cum[-1] or 1.0, cl[-1] or 1.0
        larg, j = [], 0
        for d in cl:
            t = d / totl * tot
            while j < n - 1 and cum[j + 1] < t:
                j += 1
            larg.append(max(0.9, size * fac[j]))
        # Lissage léger des largeurs (évite les marches entre segments d'origine).
        larg = [sum(larg[max(0, i - 3):i + 4]) / len(larg[max(0, i - 3):i + 4]) for i in range(len(larg))]
        for k in range(passes):
            dx = random.uniform(-tremble, tremble) if k else 0.0
            dy = random.uniform(-tremble, tremble) if k else 0.0
            contour = [[p[0] + dx, p[1] + dy] for p in _ruban(lisse, larg)]
            _selection_polygone(lay, contour)
            _remplir(lay, col, opacity if k == 0 else opacity * 0.45)
            Gimp.Selection.none(lay.get_image())
        return
    # Repli brosse : segments regroupés par épaisseur. Un trait fin (< 4 px) reste d'un seul
    # tenant : chaque groupe est composé à part, et leurs raccords feraient des perles.
    if size * max(fac) < 4.0:
        fac = [sum(fac) / len(fac)] * n
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



# -----------------------------------------------------------------------------
# Têtes d'animaux : construction complète par espèce (volumes + traits placés)
# -----------------------------------------------------------------------------
# Unité : R = rayon du crâne. Les fiches viennent de references/anatomie-animaux.md §9
# (standards de race, tutoriels de construction) et des conventions d'anatomie artistique ;
# ce qui est un choix du skill est signalé dans sources.md.
FICHES_TETES = {
    # crane : rayons (x, y, z) ; museau : (longueur, rayon base, rayon bout, piqué en °) ;
    # oreilles : (x, y, z du centre de la base, hauteur, rayon base, écartement en ° [roulis], inclinaison arrière en °) ;
    # yeux : (lon, lat, taille, ouverture, obliquité, pupille) ; joues : rayon ou None.
    "renard": {"crane": (1.0, 0.82, 1.0), "museau": (1.35, 0.40, 0.16, 18),
               "oreilles": (0.48, 0.66, 0.20, 1.20, 0.34, 24, 14),
               "yeux": (24, 10, 0.19, 0.38, 0.45, "fente"), "joues": 0.36, "larme": True,
               "truffe": 0.14, "stop": 0.10},
    "chat":   {"crane": (1.0, 0.95, 1.0), "museau": (0.45, 0.48, 0.32, 10),
               "oreilles": (0.58, 0.70, 0.0, 0.75, 0.38, 32, 8),
               "yeux": (26, 4, 0.25, 0.58, 0.25, "fente"), "joues": 0.34, "larme": False,
               "truffe": 0.11, "stop": 0.05},
    "chien":  {"crane": (1.0, 0.92, 1.15), "museau": (1.15, 0.50, 0.30, 12),
               "oreilles": (0.44, 0.80, 0.10, 1.05, 0.34, 12, 5),
               "yeux": (23, 12, 0.20, 0.40, 0.20, "ronde"), "joues": None, "larme": False,
               "truffe": 0.17, "stop": 0.18},
}
# Style : ce que le dessin s'autorise. "realiste" suit l'anatomie ; "cartoon" grossit les yeux,
# autorise le sourire et les sourcils (conventions d'animation, pas d'anatomie).
STYLES = {
    "realiste": {"yeux": 1.0, "ouverture": 1.0, "sourire": False, "sourcils": False, "tete": 1.0},
    "cartoon":  {"yeux": 1.6, "ouverture": 1.25, "sourire": True, "sourcils": True, "tete": 1.25},
}


def tete_animale(espece, centre, R, rot=(0, 0, 0), style="realiste", oreille_g=None, oreille_d=None):
    """Construit la tête d'un animal : crâne, museau, joues, oreilles, aux proportions de
    l'espèce ("renard", "chat", "chien"). Renvoie une liste de volumes à ajouter à la scène,
    plus un dict `tete` (dans volumes[0]["tete"]) qui mémorise ce qu'il faut pour
    visage_animal() : espèce, style, repère local, longueur du museau.
    centre : centre du crâne (monde, px) ; R : rayon du crâne ; rot = (lacet, tangage, roulis).
    oreille_g / oreille_d : (écartement, inclinaison arrière) en ° pour casser la symétrie
    (ex. une oreille couchée : (55, 40))."""
    f = FICHES_TETES[espece]
    st = STYLES[style]
    r = [math.radians(v) for v in rot]
    cx, cy, cz = f["crane"]
    crane = ellipsoide(f"crane_{espece}", centre, (cx * R, cy * R, cz * R), rot=rot, anneaux=2, meridiens=2)
    L, rb, rt, pique = f["museau"]
    L, rb, rt = L * R, rb * R, rt * R
    # Le museau part de l'avant du crâne, un peu sous l'équateur, et pique vers le bas.
    base = (0.0, -0.12 * R, -cz * R * 0.80)
    ax = (0.0, -math.sin(math.radians(pique)), -math.cos(math.radians(pique)))   # axe local du museau
    cen_m = tuple(base[k] + ax[k] * L / 2 for k in range(3))
    # cylindre() a son axe vertical : un tangage de -90° le couche vers l'avant, et -pique le
    # fait piquer vers le bas ; le tout composé avec l'orientation de la tête.
    museau = _cylindre_local(f"museau_{espece}", centre, r, cen_m, rb, L, (-90.0 - pique, 0.0), rt)
    vols = [crane, museau]
    if f["joues"]:
        for sx in (-1, 1):
            vols.append(_ellipsoide_local(f"joue_{'g' if sx < 0 else 'd'}", centre, r,
                                          (sx * 0.58 * R, -0.32 * R, -0.40 * R),
                                          (f["joues"] * R, f["joues"] * 0.75 * R, f["joues"] * 0.9 * R)))
    ox, oy, oz, oh, orb, ecart, arr = f["oreilles"]
    oreilles_info = {}
    for sx, perso in ((-1, oreille_g), (1, oreille_d)):
        e, a = perso if perso else (ecart, arr)
        base_o = (sx * ox * R, oy * R, oz * R)
        # Roulis positif = le haut penche vers -x : l'oreille droite (sx = +1) s'écarte avec -e.
        axe_o = _rot((0.0, 1.0, 0.0), 0.0, math.radians(a), math.radians(-sx * e))
        cen_o = tuple(base_o[k] + axe_o[k] * oh * R / 2 for k in range(3))
        vols.append(_cylindre_local(f"oreille_{'g' if sx < 0 else 'd'}", centre, r, cen_o, orb * R, oh * R,
                                    (a, -sx * e), 0.03 * R))
        oreilles_info[sx] = (base_o, axe_o, oh * R, orb * R)
    crane["tete"] = {"espece": espece, "style": style, "centre": centre, "R": R, "rot": rot,
                     "museau_base": base, "museau_axe": ax, "museau_L": L, "museau_rb": rb, "museau_rt": rt,
                     "oreilles": oreilles_info}
    return vols


def _ellipsoide_local(nom, centre, r, local, rayons):
    """Ellipsoïde dont le centre est donné dans le repère local de la tête (déjà orientée par r)."""
    return ellipsoide(nom, _place(local, centre, r), rayons, rot=[math.degrees(v) for v in r])


def _cylindre_local(nom, centre, r, local, rayon, hauteur, incl, rayon_haut):
    """Cylindre placé dans le repère local de la tête : incl = (tangage, roulis) locaux en °,
    composés avec l'orientation r de la tête."""
    rot_tete = [math.degrees(v) for v in r]
    return cylindre(nom, _place(local, centre, r), rayon, hauteur,
                    rot=(rot_tete[0], rot_tete[1] + incl[0], rot_tete[2] + incl[1]), rayon_haut=rayon_haut)


def _pt_tete(T, local):
    """Point du repère local de la tête -> pixels image."""
    r = [math.radians(v) for v in T["rot"]]
    return projeter(_place(local, T["centre"], r))


def _pt_museau(T, t, cote=0.0, haut=0.0):
    """Point sur ou autour du museau : t = 0 (base) .. 1 (bout) ; cote = -1..1 (largeur) ;
    haut = -1..1 (dessous .. dessus), en fractions du rayon local du museau."""
    rb, rt, L = T["museau_rb"], T["museau_rt"], T["museau_L"]
    rr = rb + (rt - rb) * t
    b, ax = T["museau_base"], T["museau_axe"]
    # Repère du museau : u = axe, s = latéral (x), v = vertical perpendiculaire à l'axe.
    v = (0.0, -ax[2], ax[1])          # perpendiculaire à l'axe, vers le haut
    return [b[k] + ax[k] * L * t + (1.0 if k == 0 else 0.0) * cote * rr + v[k] * haut * rr for k in range(3)]


def visage_animal(lay, crane, expression="neutre", regard=(0.0, 0.0), col="#2a2320", iris=None,
                  size=6.0, opacity=92.0):
    """Dessine les traits du visage d'une tête construite par tete_animale() : yeux (oeil()),
    truffe, bouche, marques du museau, intérieur des oreilles, selon l'espèce et le style
    mémorisés dans crane["tete"]. expression : "neutre", "endormi", "alerte", "content",
    "mefiant". En style réaliste, la bouche est la fente sous la truffe (jamais un sourire) et
    il n'y a pas de sourcils ; en cartoon, sourire et sourcils sont permis."""
    T = crane["tete"]
    f, st = FICHES_TETES[T["espece"]], STYLES[T["style"]]
    R = T["R"]
    lon, lat, taille, ouv, obl, pup = f["yeux"]
    taille *= R * st["yeux"]
    ouv *= st["ouverture"]
    ferme = expression == "endormi"
    if expression == "alerte":
        ouv *= 1.25
    if expression == "mefiant":
        ouv *= 0.7
    if expression == "content" and T["style"] == "realiste":
        ouv *= 0.85                       # un animal content plisse les yeux, il ne sourit pas
    iris = iris or {"renard": "#c98a3c", "chat": "#9bbf5a", "chien": "#6b4a2b"}[T["espece"]]
    for sx in (-1, 1):
        oeil(lay, crane, lat=lat, lon=sx * lon, taille=taille, ouverture=ouv, obliquite=obl,
             pupille=pup, iris=iris, col=col, paupiere=size * 0.9, larme=f["larme"] and not ferme,
             regard=regard, ferme=ferme, pics=(-0.15, 0.2))
    # Truffe : petit bloc au bout du museau, dessus légèrement bombé, narines.
    tr = f["truffe"] * R
    ax = T["museau_axe"]
    up = (0.0, -ax[2], ax[1])
    C = _pt_museau(T, 1.0, 0.0, 0.25)
    poly = [_pt_tete(T, [C[k] + tr * (math.cos(a) * (1.0 if k == 0 else 0.0) + 0.7 * math.sin(a) * up[k]
                                       - 0.3 * ax[k] * max(0.0, math.sin(a))) for k in range(3)])
            for a in [2 * math.pi * k / 16 for k in range(16)]]
    _selection_polygone(lay, poly)
    _remplir(lay, col)
    Gimp.Selection.none(lay.get_image())
    # Bouche : fente verticale sous la truffe puis deux lèvres qui redescendent vers l'arrière.
    p0 = _pt_tete(T, _pt_museau(T, 0.99, 0.0, -0.35))
    p1 = _pt_tete(T, _pt_museau(T, 0.95, 0.0, -0.95))
    trait(lay, courbe2d([p0, p1], 4), size=size * 0.6, col=col, opacity=opacity, profil="pointe")
    prof = {sx: _to_cam(_place(_pt_museau(T, 0.45, sx * 1.02, -0.6), T["centre"], [math.radians(v) for v in T["rot"]]))[2]
            for sx in (-1, 1)}
    proche = min(prof, key=prof.get)
    for sx in (-1, 1):
        if sx != proche and prof[sx] - prof[proche] > 0.25 * T["museau_rb"]:
            continue                                                      # commissure cachée par le museau
        if st["sourire"] and expression == "content":
            fin = _pt_tete(T, _pt_museau(T, 0.40, sx * 1.02, -0.35))      # commissure remontée : cartoon
        else:
            fin = _pt_tete(T, _pt_museau(T, 0.45, sx * 1.02, -0.85))      # commissure qui descend : réaliste
        mil = _pt_tete(T, _pt_museau(T, 0.72, sx * 0.98, -0.92))
        trait(lay, courbe2d([p1, mil, fin], 6), size=size * 0.55, col=col, opacity=opacity * 0.85, profil="pointe")
    # Coussinets de moustaches : 3 petits points de chaque côté, sur le dessus du museau.
    for sx in (-1, 1):
        for k in range(3):
            q = _pt_tete(T, _pt_museau(T, 0.78 - 0.1 * k, sx * (0.55 + 0.15 * k), 0.15))
            trait(lay, [q, [q[0] + 0.5, q[1]]], size=size * 0.45, col=col, opacity=55)
    # Intérieur des oreilles : une ligne qui longe le bord avant, du haut vers la base.
    for sx, (base_o, axe_o, h_o, r_o) in T["oreilles"].items():
        pointe = [base_o[k] + axe_o[k] * h_o * 0.92 for k in range(3)]
        avant = [base_o[k] - r_o * 0.75 * (1.0 if k == 2 else 0.0) + axe_o[k] * h_o * 0.12 for k in range(3)]
        milieu = [(pointe[k] + avant[k]) / 2 + (r_o * 0.25 if k == 0 else 0.0) * sx for k in range(3)]
        trait(lay, courbe2d([_pt_tete(T, pointe), _pt_tete(T, milieu), _pt_tete(T, avant)], 5),
              size=size * 0.45, col=col, opacity=65, profil="fuseau")
    # Sourcils : cartoon seulement (un animal réel n'a pas de sourcils tracés).
    if st["sourcils"] and expression in ("alerte", "mefiant", "content"):
        for sx in (-1, 1):
            a = _pt_tete(T, (sx * 0.22 * R, 0.50 * R, -0.84 * R))
            b = _pt_tete(T, (sx * 0.58 * R, (0.60 if expression != "mefiant" else 0.42) * R, -0.70 * R))
            trait(lay, courbe2d([a, b], 4), size=size * 0.7, col=col, profil="fuseau")
    Gimp.displays_flush()


# -----------------------------------------------------------------------------
# Segments 3D (membres), tête et figure humaines
# -----------------------------------------------------------------------------

def segment(nom, A, B, rayon, rayon_fin=None):
    """Cylindre (ou tronc de cône) qui va du point 3D A au point 3D B : un os, un membre,
    un cou, un manche. rayon à A, rayon_fin à B (défaut : le même)."""
    d = [B[k] - A[k] for k in range(3)]
    L = math.sqrt(sum(v * v for v in d)) or 1.0
    d = [v / L for v in d]
    # L'axe local de cylindre() est y ; on cherche (tangage, roulis) tels que l'axe tombe sur d.
    roulis = -math.asin(max(-1.0, min(1.0, d[0])))
    c = math.cos(roulis)
    tangage = math.atan2(d[2], d[1]) if abs(c) > 1e-6 else 0.0
    centre = tuple((A[k] + B[k]) / 2 for k in range(3))
    return cylindre(nom, centre, rayon, L, rot=(0.0, math.degrees(tangage), math.degrees(roulis)),
                    rayon_haut=rayon_fin)


def tete_humaine(centre, R, rot=(0, 0, 0), sexe="h"):
    """Tête humaine construite à la Loomis : boule du crâne (rayon R), plans latéraux,
    mâchoire, cou. Renvoie des volumes à ajouter à la scène ; volumes[0]["tete"] mémorise
    les repères pour visage_humain(). Repères (centre de la boule = ligne des sourcils) :
    tiers du visage = 2R/3 ; base du nez à -2R/3 ; menton à -4R/3 ; ligne des cheveux à
    +2R/3 ; yeux juste sous les sourcils (à mi-hauteur de la tête) ; oreilles entre sourcils
    et base du nez. Sources : references/tete-et-yeux.md §1."""
    r = [math.radians(v) for v in rot]
    T3 = 2.0 * R / 3.0
    crane = ellipsoide("crane", centre, (0.92 * R, R, R), rot=rot, anneaux=2, meridiens=2)
    # Mâchoire : boîte biseautée approchée par un ellipsoïde aplati qui descend jusqu'au menton.
    machoire = _ellipsoide_local("machoire", centre, r, (0.0, -0.78 * R, -0.18 * R),
                                 ((0.80 if sexe == "h" else 0.74) * R, 0.60 * R, 0.80 * R))
    cou = segment("cou", _place((0.0, -1.15 * R, 0.25 * R), centre, r), _place((0.0, -1.9 * R, 0.35 * R), centre, r),
                  (0.42 if sexe == "h" else 0.34) * R)
    crane["tete"] = {"espece": "humain", "style": "realiste", "centre": centre, "R": R, "rot": rot, "sexe": sexe}
    return [crane, machoire, cou]


def visage_humain(lay, crane, expression="neutre", regard=(0.0, 0.0), col="#2a2320", iris="#6b5a4a",
                  size=5.0, opacity=92.0, cheveux=True):
    """Traits d'une tête construite par tete_humaine(), placés par les proportions Loomis :
    sourcils, yeux (largeur = 1/5 de la tête, un œil d'écart), nez (base à un tiers sous les
    sourcils, largeur = un œil), bouche (à un tiers entre nez et menton, largeur = écart des
    pupilles), oreilles (entre sourcils et base du nez), mâchoire, ligne des cheveux.
    expression : "neutre", "sourire" (léger, commissures relevées et yeux un peu plissés),
    "serieux" (sourcils rapprochés), "surpris" (yeux ouverts, sourcils hauts), "endormi"."""
    T = crane["tete"]
    R, centre = T["R"], T["centre"]
    r = [math.radians(v) for v in T["rot"]]
    P = lambda loc: projeter(_place(loc, centre, r))
    T3 = 2.0 * R / 3.0
    oe = 0.18 * R                                       # demi-largeur d'un œil (tête = 5 yeux)
    y_yeux = -0.17 * R                                  # mi-hauteur de la tête (sommet +R, menton -4R/3)
    ouv = {"neutre": 0.42, "sourire": 0.34, "serieux": 0.38, "surpris": 0.6, "endormi": 0.3}[expression]
    for sx in (-1, 1):
        lon = math.degrees(math.asin(0.36))             # centre de l'œil à ±0,36 R (un œil d'écart)
        lat = math.degrees(math.asin(y_yeux / R))
        oeil(lay, crane, lat=lat, lon=sx * lon, taille=oe, ouverture=ouv, obliquite=0.05, regard=regard,
             pupille="ronde", iris=iris, col=col, paupiere=size * 0.9, ferme=(expression == "endormi"))
    # Sourcils : sur la ligne des sourcils (y = 0), du coin interne au-delà du coin externe.
    hb = {"neutre": 0.08, "sourire": 0.09, "serieux": 0.02, "surpris": 0.2, "endormi": 0.06}[expression]
    for sx in (-1, 1):
        a = P((sx * 0.17 * R, hb * R + (0.0 if expression != "serieux" else -0.04 * R), -0.93 * R))
        m = P((sx * 0.40 * R, (hb + 0.05) * R, -0.86 * R))
        b = P((sx * 0.62 * R, (hb + 0.01) * R, -0.72 * R))
        trait(lay, courbe2d([a, m, b], 5), size=size * 0.9, col=col, opacity=opacity, profil="pointe")
    # Nez : arête côté ombre (gauche de l'image = côté éloigné de la lumière par défaut), bout, narines.
    base = P((0.0, -T3, -1.02 * R))
    arete = [P((0.06 * R, -0.05 * R, -0.95 * R)), P((0.12 * R, -0.35 * R, -1.02 * R)), P((0.14 * R, -0.58 * R, -1.08 * R))]
    trait(lay, courbe2d(arete, 5), size=size * 0.5, col=col, opacity=70, profil="pointe")
    bout = courbe2d([P((0.16 * R, -0.58 * R, -1.08 * R)), P((0.0, -0.66 * R, -1.09 * R)), P((-0.16 * R, -0.58 * R, -1.08 * R))], 6)
    trait(lay, bout, size=size * 0.6, col=col, opacity=opacity, profil="fuseau")
    for sx in (-1, 1):
        nar = courbe2d([P((sx * 0.18 * R, -0.60 * R, -1.0 * R)), P((sx * 0.14 * R, -0.67 * R, -1.02 * R)), P((sx * 0.06 * R, -0.68 * R, -1.06 * R))], 5)
        trait(lay, nar, size=size * 0.5, col=col, opacity=80, profil="fuseau")
    # Bouche : ligne de séparation des lèvres à un tiers entre nez et menton, largeur = écart des pupilles.
    y_b = -T3 - T3 / 3.0
    lev = 0.06 * R if expression == "sourire" else 0.0
    bouche = [P((-0.36 * R, y_b + lev, -0.92 * R)), P((-0.12 * R, y_b - 0.015 * R, -1.0 * R)),
              P((0.12 * R, y_b - 0.015 * R, -1.0 * R)), P((0.36 * R, y_b + lev, -0.92 * R))]
    trait(lay, courbe2d(bouche, 8), size=size * 0.8, col=col, opacity=opacity, profil="fuseau")
    # Ombre sous la lèvre inférieure : un court trait, pas un contour.
    trait(lay, courbe2d([P((-0.10 * R, y_b - 0.17 * R, -0.98 * R)), P((0.0, y_b - 0.19 * R, -1.0 * R)), P((0.10 * R, y_b - 0.17 * R, -0.98 * R))], 5),
          size=size * 0.4, col=col, opacity=45, profil="fuseau")
    # Oreilles : de la ligne des sourcils à la base du nez, sur le plan latéral.
    for sx in (-1, 1):
        ore = courbe2d([P((sx * 0.92 * R, 0.0, 0.05 * R)), P((sx * 1.02 * R, -0.25 * R, 0.1 * R)),
                        P((sx * 0.96 * R, -0.6 * R, 0.08 * R)), P((sx * 0.86 * R, -0.66 * R, 0.0))], 6)
        trait(lay, ore, size=size * 0.6, col=col, opacity=80, profil="fuseau")
    # Mâchoire : de sous l'oreille au menton, et ligne des cheveux.
    for sx in (-1, 1):
        mach = courbe2d([P((sx * 0.88 * R, -0.62 * R, -0.05 * R)), P((sx * 0.70 * R, -1.05 * R, -0.45 * R)),
                         P((sx * 0.30 * R, -1.30 * R, -0.80 * R)), P((0.0, -1.34 * R, -0.88 * R))], 6)
        trait(lay, mach, size=size * (0.9 if sx < 0 else 0.5), col=col, opacity=opacity, profil="fuseau")
    if cheveux:
        ch = courbe2d([P((-0.75 * R, 0.45 * R, -0.55 * R)), P((-0.4 * R, 0.68 * R, -0.78 * R)), P((0.0, 0.70 * R, -0.85 * R)),
                       P((0.4 * R, 0.68 * R, -0.78 * R)), P((0.75 * R, 0.45 * R, -0.55 * R))], 6)
        trait(lay, ch, size=size * 0.5, col=col, opacity=60, profil="fuseau")
    Gimp.displays_flush()


def figure_humaine(sol_xz=(0.0, 0.0), H=100.0, tetes=8.0, appui="droite", bras=((20, 10), (20, 10)),
                   tete_rot=(0, 0, 0), sexe="h"):
    """Figure debout construite en volumes 3D aux proportions en têtes (H = hauteur d'une tête ;
    tetes = 8 idéalisé Loomis, 7,5 moyen Richer). Repères (depuis le sol, en H) : genou 2,1 ;
    entrejambe tetes-4 ; nombril tetes-3 ; mamelons tetes-2 ; menton tetes-1 ; épaules
    (acromion) tetes-1,5 ; largeur d'épaules 2 H (2⅓ idéalisé), hanches 1,5 H ; coude au
    nombril, poignet à l'entrejambe, bout des doigts à mi-cuisse.
    appui : "droite" / "gauche" / "deux" : la jambe qui porte le poids ; le bassin monte de ce
    côté et l'épaule descend (contrapposto), la jambe libre fléchit un peu.
    bras : ((élévation, flexion du coude), ...) en degrés pour le bras gauche puis droit.
    Renvoie (volumes, joints) ; joints = dict nom -> point 3D, à projeter avec projeter()
    pour dessiner les membres avec patte()."""
    x0, z0 = sol_xz
    larg_ep = ((2.33 if tetes >= 8 else 2.0) * H / 2 if sexe == "h" else 1.75 * H / 2) - 0.22 * H   # acromion (le deltoïde ajoute le reste)
    larg_ha = 0.75 * H if sexe == "h" else 0.8 * H
    y_ent, y_nomb, y_mam, y_ment = (tetes - 4) * H, (tetes - 3) * H, (tetes - 2) * H, (tetes - 1) * H
    y_ep = (tetes - 1.5) * H
    # Contrapposto : côté d'appui -> hanche haute, épaule basse.
    sa = {"droite": 1, "gauche": -1, "deux": 0}[appui]
    tilt_h, tilt_e = 0.07 * H * sa, -0.05 * H * sa
    J = {}
    J["tete"] = (x0, (tetes - 0.5) * H, z0)
    J["cou_h"] = (x0, y_ment - 0.05 * H, z0 + 0.05 * H)
    J["cou_b"] = (x0, y_ep + 0.15 * H, z0 + 0.05 * H)
    for sx, nom in ((-1, "g"), (1, "d")):
        ha = (x0 + sx * 0.4 * H, y_ent + 0.1 * H + sx * tilt_h, z0)
        # Jambe d'appui verticale ; jambe libre : genou un peu en avant et pied un peu décalé.
        libre = (sa != 0 and sx != sa)
        ge = (ha[0] + (sx * 0.05 * H), 2.1 * H, z0 - (0.25 * H if libre else 0.0))
        ch = (ha[0] + sx * 0.08 * H, 0.25 * H, z0 - (0.15 * H if libre else 0.0))
        pi = (ch[0] + sx * 0.05 * H, 0.0, ch[2] - 0.45 * H)
        J[f"hanche_{nom}"], J[f"genou_{nom}"], J[f"cheville_{nom}"], J[f"pied_{nom}"] = ha, ge, ch, pi
        ep = (x0 + sx * larg_ep, y_ep + sx * tilt_e, z0)
        el, fl = (math.radians(v) for v in bras[0 if sx < 0 else 1])
        co = (ep[0] + sx * math.sin(el) * 1.5 * H * 0.9, ep[1] - math.cos(el) * 1.5 * H * 0.9, z0 + 0.05 * H)
        po = (co[0] + sx * math.sin(el) * 1.2 * H * 0.9, co[1] - math.cos(el + fl) * 1.2 * H * 0.9, z0 - math.sin(fl) * 1.2 * H * 0.9)
        ma = (po[0] + (po[0] - co[0]) * 0.45, po[1] + (po[1] - co[1]) * 0.45, po[2] + (po[2] - co[2]) * 0.45)
        J[f"epaule_{nom}"], J[f"coude_{nom}"], J[f"poignet_{nom}"], J[f"main_{nom}"] = ep, co, po, ma
    V = tete_humaine(J["tete"], 0.43 * H, rot=tete_rot, sexe=sexe)
    V.append(ellipsoide("thorax", (x0, y_mam + 0.1 * H, z0), (0.82 * H, 1.0 * H, 0.52 * H), rot=(0, 0, -tilt_e / H * 25)))
    V.append(ellipsoide("abdomen", (x0, y_nomb + 0.05 * H, z0), (0.62 * H, 0.42 * H, 0.42 * H)))
    V.append(boite("bassin", (x0, y_ent + 0.5 * H, z0), (larg_ha * 2 * 0.95, 0.9 * H, 0.7 * H), rot=(0, 0, tilt_h / H * 25)))
    for sx, nom in ((-1, "g"), (1, "d")):   # clavicules : du creux du cou à l'acromion
        V.append(segment(f"clavicule_{nom}", (x0 + sx * 0.12 * H, y_ep + 0.05 * H, z0 - 0.3 * H), J[f"epaule_{nom}"], 0.06 * H))
    for nom in ("g", "d"):
        V.append(segment(f"cuisse_{nom}", J[f"hanche_{nom}"], J[f"genou_{nom}"], 0.30 * H, 0.19 * H))
        V.append(segment(f"jambe_{nom}", J[f"genou_{nom}"], J[f"cheville_{nom}"], 0.19 * H, 0.11 * H))
        V.append(ellipsoide(f"pied_{nom}", ((J[f"cheville_{nom}"][0] + J[f"pied_{nom}"][0]) / 2, 0.12 * H,
                                            (J[f"cheville_{nom}"][2] + J[f"pied_{nom}"][2]) / 2), (0.17 * H, 0.12 * H, 0.42 * H)))
        V.append(segment(f"bras_{nom}", J[f"epaule_{nom}"], J[f"coude_{nom}"], 0.19 * H, 0.14 * H))
        V.append(segment(f"avant_bras_{nom}", J[f"coude_{nom}"], J[f"poignet_{nom}"], 0.15 * H, 0.09 * H))
        V.append(segment(f"main_{nom}", J[f"poignet_{nom}"], J[f"main_{nom}"], 0.12 * H, 0.05 * H))
    for sx, nom in ((-1, "g"), (1, "d")):   # deltoïdes : épaules rondes, pas des tubes qui sortent d'un œuf
        V.append(ellipsoide(f"deltoide_{nom}", J[f"epaule_{nom}"], (0.24 * H, 0.26 * H, 0.24 * H)))
    return V, J


def joints2d(J):
    """Projette le dict de joints 3D de figure_humaine() en pixels image."""
    return {k: projeter(v) for k, v in J.items()}


# -----------------------------------------------------------------------------
# Natures mortes : objets de révolution, ombre portée
# -----------------------------------------------------------------------------

def objet_revolution(nom, base, profil, anneaux_par_segment=1):
    """Objet tourné (bouteille, vase, tasse, verre, pomme sur pied) : profil = liste de
    (rayon, hauteur depuis le sol) du bas vers le haut ; chaque intervalle devient un tronc
    de cône. base = (x, y, z) du centre de la base. Renvoie la liste des volumes.
    Ex. bouteille : [(60, 0), (62, 180), (40, 230), (18, 260), (18, 330)]."""
    vols = []
    for i in range(len(profil) - 1):
        (r0, h0), (r1, h1) = profil[i], profil[i + 1]
        if h1 <= h0:
            continue
        vols.append(cylindre(f"{nom}_{i}", (base[0], base[1] + (h0 + h1) / 2, base[2]), r0, h1 - h0,
                             rayon_haut=r1, anneaux=anneaux_par_segment))
    for v in vols:                  # géométrie gardée : surface() et modele_doux() s'en servent
        v["revolution"] = {"base": base, "profil": profil}
    return vols


# -----------------------------------------------------------------------------
# Surface paramétrée et modelé doux (rendu au crayon par valeurs, pas en trame)
# -----------------------------------------------------------------------------

def surface(objet, u, v):
    """Point et normale sur la surface d'un objet, en coordonnées monde.
    objet : un ellipsoïde, ou la liste renvoyée par objet_revolution().
    u : tour en degrés (0-360) ; v : hauteur de 0 (bas) à 1 (haut).
    Sert à poser sur la forme ce qui doit la suivre : bande de valeur, reflet, motif,
    hachure qui s'enroule. Renvoie ((x, y, z), (nx, ny, nz))."""
    th = math.radians(u)
    if isinstance(objet, list):                      # objet de révolution
        rev = objet[0]["revolution"]
        prof, b = rev["profil"], rev["base"]
        h = prof[0][1] + (prof[-1][1] - prof[0][1]) * v
        for (r0, h0), (r1, h1) in zip(prof[:-1], prof[1:]):
            if h0 <= h <= h1 and h1 > h0:
                t = (h - h0) / (h1 - h0)
                r, pente = r0 + (r1 - r0) * t, (r1 - r0) / (h1 - h0)
                break
        else:
            r, pente = prof[-1][0], 0.0
        n = (math.cos(th), -pente, math.sin(th))
        l = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        return ((b[0] + r * math.cos(th), b[1] + h, b[2] + r * math.sin(th)),
                (n[0] / l, n[1] / l, n[2] / l))
    a, bb, c = objet["rayons"]                        # ellipsoïde
    r = [math.radians(x) for x in objet["rot"]]
    phi = -math.pi / 2 + math.pi * v
    loc = (a * math.cos(phi) * math.cos(th), bb * math.sin(phi), c * math.cos(phi) * math.sin(th))
    nl = _rot((loc[0] / (a * a), loc[1] / (bb * bb), loc[2] / (c * c)), *r)
    l = math.sqrt(sum(x * x for x in nl)) or 1.0
    return _place(loc, objet["centre"], r), (nl[0] / l, nl[1] / l, nl[2] / l)


def modele_doux(lay, objet, lumiere=(-1.0, -1.4, -0.9), col="#3a3330", valeur_locale=0.0,
                force=60.0, reflet=0.35, durete=0.15, hachures=0, graine=None, papier=None):
    """Modelé au crayon d'un volume : le dégradé lumière -> demi-teinte -> terminateur ->
    lumière réfléchie, posé en passes douces qui se recouvrent (comme l'estompe), rogné par la
    silhouette. C'est le rendu par VALEURS : à préférer aux trames de hachures qui couvrent
    tout l'objet (effet « gravure »).
    objet : ellipsoïde ou liste d'objet_revolution ; lumiere : même vecteur qu'ombre_portee
      (passe LE MÊME aux deux) ; z négatif = source du côté du spectateur (éclairage de 3/4 avant,
      le défaut) ; z positif = contre-jour, l'objet est presque tout dans l'ombre.
    papier : couleur du fond pour couvrir d'abord la silhouette (cache l'ombre portée dessous).
    valeur_locale : 0 (objet blanc) à 1 (objet noir) ; une pomme rouge ou un bois moyen ≈ 0,35,
      elle assombrit tout l'objet, même côté lumière.
    force : opacité de la passe la plus sombre ; reflet : part de lumière rendue côté ombre près
      du bord (table claire qui renvoie la lumière) ; durete : 0,1-0,3 (doux) à 0,6 (net).
    hachures : nombre de hachures ajoutées SEULEMENT dans l'ombre propre (0 = aucune ; 15-40
      pour un crayon vivant), de longueurs et d'espacements irréguliers, qui suivent la forme.
    Pose le modelé AVANT le contour (calque Valeurs sous Croquis/Encrage)."""
    rnd = random.Random(graine)
    vols = objet if isinstance(objet, list) else [objet]
    lx, ly, lz = lumiere
    s = (lx, -ly, lz)                                   # direction vers la source
    ls = math.sqrt(sum(x * x for x in s)) or 1.0
    s = [x / ls for x in s]
    # 1. Rogner par la silhouette (union des volumes) : rien ne déborde.
    for k, v in enumerate(vols):
        _selection_polygone(lay, v["silhouette"], "replace" if k == 0 else "add")
    if papier:
        _remplir(lay, papier)
    if valeur_locale > 0:
        _remplir(lay, col, 100.0 * valeur_locale * 0.6)
    # 2. Échantillonner la surface : méridiens, valeur en chaque point visible.
    NU, NV = 72, 36
    meridiens = []
    for i in range(NU):
        pts = []
        for j in range(NV + 1):
            # Ellipsoïde : on évite les pôles, où tous les méridiens se rejoignent (tache).
            v = j / NV if isinstance(objet, list) else 0.04 + 0.92 * j / NV
            p, n = surface(objet, 360.0 * i / NU, v)
            pc, nc = _to_cam(p), _to_cam(n, translate=False)
            if not _visible(pc, nc):
                pts.append(None)
                continue
            lam = n[0] * s[0] + n[1] * s[1] + n[2] * s[2]          # -1 (dos à la source) .. 1
            # Sombre = 1 - éclairement ; terminateur un peu plus sombre que l'ombre (cœur d'ombre),
            # puis lumière réfléchie vers le bord côté ombre.
            d = 0.5 - 0.5 * lam
            if lam < 0:
                vue = abs(nc[2]) / (math.sqrt(sum(x * x for x in nc)) or 1.0)   # 1 = face à nous
                d -= reflet * (1.0 - vue) * min(1.0, -lam * 2.5) * 0.6
                d += 0.12 * math.exp(-((lam + 0.12) / 0.12) ** 2)        # cœur d'ombre
            pts.append((_proj(pc), max(0.0, min(1.0, d))))
        meridiens.append(pts)
    # Pas entre méridiens à l'écran -> taille de la brosse (les passes doivent se fondre).
    xs = [q[0][0] for m in meridiens for q in m if q]
    if not xs:
        Gimp.Selection.none(lay.get_image())
        return
    taille = max(6.0, (max(xs) - min(xs)) / (NU / 2) * 2.6)
    # 3. Passes : chaque seuil franchi ajoute une couche douce (accumulation = dégradé).
    Gimp.context_push()
    Gimp.context_set_foreground(color(col))
    Gimp.context_set_brush_hardness(durete)
    Gimp.context_set_brush_size(taille)
    # 10 seuils de 0,12 à 0,80 : à force=60, une ombre propre atteint ~50 % de noir (papier
    # blanc) ; le terminateur un peu plus ; seul le point le plus éclairé reste le papier.
    for k in range(10):
        seuil = 0.12 + 0.68 * k / 9
        Gimp.context_set_opacity(force * (0.06 if seuil < 0.4 else 0.11))
        for m in meridiens:
            run = []
            for q in m + [None]:
                if q and q[1] >= seuil:
                    run.append(q[0])
                elif run:
                    if len(run) > 1:            # un point isolé ferait une tache
                        Gimp.paintbrush_default(lay, [c for pt in run for c in pt])
                    run = []
    Gimp.context_pop()
    # 4. Quelques hachures dans l'ombre propre seulement, irrégulières, le long des parallèles.
    ombre = [(i, j) for i, m in enumerate(meridiens) for j, q in enumerate(m) if q and q[1] >= 0.62]
    for _ in range(int(hachures)):
        if not ombre:
            break
        i, j = rnd.choice(ombre)
        lg = rnd.randint(3, 8)
        pts = []
        for k in range(lg):
            q = meridiens[(i + k) % NU][j]
            if not q:
                break
            pts.append(q[0])
        if len(pts) >= 3:
            trait(lay, courbe2d(pts), size=rnd.uniform(1.8, 3.2), col=col, opacity=rnd.uniform(35, 60),
                  profil="fuseau", hardness=0.5)
    Gimp.Selection.none(lay.get_image())


def ombre_portee(lay, volumes, lumiere=(-1.0, -1.6, 0.6), sol_y=0.0, col="#2a2320", pas=9.0, angle=35.0,
                 size=2.5, opacity=60.0, papier=None, ton=None):
    """Ombre portée au sol d'un ou plusieurs volumes, calculée en projetant leur surface le
    long de la direction de la lumière (vecteur 3D : d'où elle VIENT ; (-1, -1.6, 0.6) =
    en haut à gauche, un peu derrière). L'ombre est remplie de hachures parallèles ;
    papier = couleur pour l'aplatir d'abord (ombre pleine claire) ; ton = 0-1 : ombre en aplat de
    valeur, sans hachures (rendu crayon par valeurs, à préférer hors gravure/encre).
    Dessine les ombres AVANT les objets (ou sur un calque dessous) : l'ombre passe sous eux."""
    lx, ly, lz = lumiere
    if ly >= 0:
        ly = -1e-3
    pts = []
    for v in volumes:
        for p in v.get("surf3d", []):
            t = (p[1] - sol_y) / (-ly)                       # distance le long du rayon jusqu'au sol
            pts.append(_proj(_to_cam((p[0] - lx * t, sol_y, p[2] - lz * t))))   # on s'éloigne de la source
    if len(pts) < 3:
        return
    poly = _hull(pts)
    _selection_polygone(lay, poly)
    if papier:
        _remplir(lay, papier)
    if ton is not None:
        # Rendu crayon par valeurs : un aplat (ton = 0-1) au lieu de la trame de hachures ;
        # l'ombre de contact (trait épais au pied de l'objet) se pose ensuite à part.
        _remplir(lay, col, 100.0 * ton * 0.6)
        Gimp.Selection.none(lay.get_image())
        return
    # Hachures dans la sélection : lignes parallèles qui couvrent la boîte englobante.
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    a = math.radians(angle)
    dx, dy = math.cos(a), math.sin(a)
    nx, ny = -dy, dx
    diag = math.hypot(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    n = int(diag / pas) + 1
    for i in range(-n // 2, n // 2 + 1):
        ox, oy = cx + nx * i * pas + random.uniform(-1, 1), cy + ny * i * pas
        trait(lay, [[ox - dx * diag, oy - dy * diag], [ox + dx * diag, oy + dy * diag]],
              size=size, col=col, opacity=opacity, profil="fuseau")
    Gimp.Selection.none(lay.get_image())
    # Contour de l'ombre : plus appuyé près de l'objet (contact), perdu à l'extrémité.
    trait(lay, poly + [poly[0]], size=size * 0.8, col=col, opacity=opacity * 0.6, profil="constant")

result = ("volumes3d chargé : camera, cadrer, ellipsoide, boite, cylindre, par_nom, scene, sol, "
          "projeter, courbe3d, courbe2d, trait, bord_fourrure, contours_visibles, croquis_auto, "
          "oeil, tete_animale, visage_animal, tete_humaine, visage_humain, figure_humaine, joints2d, "
          "segment, objet_revolution, ombre_portee, membre_quadrupede, patte, pied")
